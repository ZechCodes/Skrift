import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env file early so env vars are available for YAML interpolation
# Load from current working directory (where app.yaml lives)
_env_file = Path.cwd() / ".env"
load_dotenv(_env_file)

# Pattern to match $VAR_NAME environment variable references
ENV_VAR_PATTERN = re.compile(r"\$([A-Z_][A-Z0-9_]*)")

# Environment configuration
SKRIFT_ENV = "SKRIFT_ENV"
DEFAULT_ENVIRONMENT = "production"

# Override for config file path (set via CLI -f flag)
_config_path_override: Path | None = None


def set_config_path(path: Path) -> None:
    """Set an explicit config file path, overriding environment-based resolution."""
    global _config_path_override
    _config_path_override = path


def get_environment() -> str:
    """Get the current environment name, normalized to lowercase.

    Reads from SKRIFT_ENV environment variable. Defaults to "production".
    """
    env = os.environ.get(SKRIFT_ENV, DEFAULT_ENVIRONMENT)
    return env.lower().strip()


def get_config_path() -> Path:
    """Get the path to the config file.

    If set_config_path() was called, returns that path.
    Otherwise: production -> app.yaml, other envs -> app.{env}.yaml
    """
    if _config_path_override is not None:
        return _config_path_override

    env = get_environment()
    if env == "production":
        return Path.cwd() / "app.yaml"
    return Path.cwd() / f"app.{env}.yaml"


def interpolate_env_vars(value, strict: bool = True, _path: str = ""):
    """Recursively replace $VAR_NAME with os.environ values.

    Args:
        value: The value to interpolate
        strict: If True, raise an error when env var is not set.
                If False, return the original $VAR_NAME reference.
        _path: Internal — dot-separated YAML key path for error messages.
    """
    if isinstance(value, str):

        def replace(match):
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                if strict:
                    location = f" (in {_path})" if _path else ""
                    raise ValueError(
                        f"Environment variable ${var} is not set{location}.\n"
                        f"  Hint: Set it in your .env file or shell environment."
                    )
                return match.group(0)  # Return original $VAR_NAME
            return val

        return ENV_VAR_PATTERN.sub(replace, value)
    elif isinstance(value, dict):
        return {
            k: interpolate_env_vars(v, strict, f"{_path}.{k}" if _path else k)
            for k, v in value.items()
        }
    elif isinstance(value, list):
        return [
            interpolate_env_vars(item, strict, f"{_path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value


def load_app_config(interpolate: bool = True, strict: bool = True) -> dict:
    """Load and parse app.yaml with optional environment variable interpolation.

    Args:
        interpolate: Whether to interpolate environment variables
        strict: If interpolating, whether to raise errors for missing env vars

    Returns:
        Parsed configuration dictionary
    """
    config_path = get_config_path()

    if not config_path.exists():
        raise FileNotFoundError(f"{config_path.name} not found at {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    if interpolate:
        return interpolate_env_vars(config, strict=strict)
    return config


def load_raw_app_config() -> dict | None:
    """Load app.yaml without any processing. Returns None if file doesn't exist."""
    config_path = get_config_path()

    if not config_path.exists():
        return None

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_model_modules() -> list[str]:
    """Load model module paths from app.yaml `models` key."""
    config = load_raw_app_config()
    if config is None:
        return []
    return config.get("models", [])


class PageTypeConfig(BaseModel):
    """Configuration for a page type."""

    name: str        # "post"
    plural: str      # "posts"
    icon: str = "file-text"
    nav_order: int = 20
    subdomain: str = ""  # When set, type is served on this subdomain only


DEFAULT_PAGE_TYPES = [
    PageTypeConfig(name="page", plural="pages", icon="file-text", nav_order=20),
]


def load_page_types_from_yaml() -> list[PageTypeConfig]:
    """Load page type definitions from app.yaml.

    Ensures the "page" type always exists.
    """
    config = load_raw_app_config()
    if config is None or "page_types" not in config:
        return list(DEFAULT_PAGE_TYPES)
    types = [PageTypeConfig(**pt) for pt in config["page_types"]]
    if not any(t.name == "page" for t in types):
        types = [DEFAULT_PAGE_TYPES[0], *types]
    return types


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    url: str = "sqlite+aiosqlite:///./app.db"
    pool_size: int = 5
    pool_overflow: int = 10
    pool_timeout: int = 30
    pool_pre_ping: bool = True  # Validate connections before use
    echo: bool = False
    db_schema: str | None = Field(default=None, validation_alias="schema")


class OAuthProviderConfig(BaseModel):
    """OAuth provider configuration."""

    client_id: str
    client_secret: str
    scopes: list[str] = ["openid", "email", "profile"]
    # Optional tenant ID for Microsoft/Azure AD
    tenant_id: str | None = None


class DummyProviderConfig(BaseModel):
    """Dummy provider configuration (no credentials required)."""

    pass


class SkriftProviderConfig(BaseModel):
    """Skrift OAuth provider config (points at a remote Skrift OAuth2 server)."""

    server_url: str
    client_id: str
    client_secret: str = ""
    scopes: list[str] = ["openid", "profile", "email"]


# Union type for provider configs - dummy has no required fields
ProviderConfig = OAuthProviderConfig | DummyProviderConfig | SkriftProviderConfig


class SecondFactorSettings(BaseModel):
    """Second-factor authentication configuration."""

    enabled: bool = False
    challenge_on_enrolled: bool = False
    methods: dict[str, dict] = {}

    def get_method_keys(self) -> list[str]:
        """Return configured second-factor method keys."""
        return list(self.methods.keys())

    def get_method_type(self, key: str) -> str:
        """Resolve the configured second-factor method type."""
        config = self.methods.get(key, {})
        if isinstance(config, dict):
            return config.get("type", "") or key
        return key

    def get_method_config(self, key: str) -> dict:
        """Get the raw config dict for a second-factor method key."""
        config = self.methods.get(key, {})
        return dict(config) if isinstance(config, dict) else {}


def _method_config_from_provider_config(name: str, config: dict) -> dict:
    """Derive an auth.methods entry from a legacy auth.providers entry."""
    config = dict(config)
    provider_type = config.get("provider", "") or name
    method_type = "dummy" if provider_type == "dummy" else "oauth"
    method_config = dict(config)
    method_config["type"] = method_type
    if method_type == "oauth" and provider_type != name:
        method_config["provider"] = provider_type
    elif method_type == "dummy":
        method_config.pop("provider", None)
    return method_config


def _provider_config_from_method_config(name: str, config: dict) -> dict | None:
    """Derive an auth.providers entry from an auth.methods entry when possible."""
    config = dict(config)
    method_type = config.pop("type", "") or "oauth"

    if method_type == "dummy":
        return {"provider": "dummy"}
    if method_type == "oauth":
        provider_type = config.get("provider", "") or name
        provider_config = dict(config)
        if provider_type != name:
            provider_config["provider"] = provider_type
        return provider_config
    return None


def get_auth_method_configs(auth_config: dict | None) -> dict[str, dict]:
    """Normalize raw auth config to auth.methods shape."""
    if not auth_config:
        return {}

    methods = auth_config.get("methods", {})
    providers = auth_config.get("providers", {})
    normalized: dict[str, dict] = {}

    if isinstance(methods, dict):
        for name, config in methods.items():
            if isinstance(config, dict):
                normalized[name] = dict(config)

    if isinstance(providers, dict):
        for name, config in providers.items():
            if name in normalized or not isinstance(config, dict):
                continue
            normalized[name] = _method_config_from_provider_config(name, config)

    return normalized


def get_auth_provider_configs(auth_config: dict | None) -> dict[str, dict]:
    """Normalize raw auth config to auth.providers shape for OAuth-compatible code."""
    if not auth_config:
        return {}

    providers = auth_config.get("providers", {})
    normalized: dict[str, dict] = {}

    if isinstance(providers, dict):
        for name, config in providers.items():
            if isinstance(config, dict):
                normalized[name] = dict(config)

    methods = auth_config.get("methods", {})
    if isinstance(methods, dict):
        for name, config in methods.items():
            if name in normalized or not isinstance(config, dict):
                continue
            provider_config = _provider_config_from_method_config(name, config)
            if provider_config is not None:
                normalized[name] = provider_config

    return normalized


class SecurityHeadersConfig(BaseModel):
    """Security response headers configuration.

    Each header can be set to None or empty string to disable it.
    Setting enabled=False disables the entire middleware.
    """

    enabled: bool = True
    content_security_policy: str | None = "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; img-src 'self' data: https:; font-src 'self' https:; script-src 'self'; form-action 'self'; base-uri 'self'"
    csp_nonce: bool = True
    strict_transport_security: str | None = "max-age=63072000; includeSubDomains"
    x_content_type_options: str | None = "nosniff"
    x_frame_options: str | None = "DENY"
    referrer_policy: str | None = "strict-origin-when-cross-origin"
    permissions_policy: str | None = "camera=(), microphone=(), geolocation=()"
    cross_origin_opener_policy: str | None = "same-origin"
    cross_origin_resource_policy: str | None = "same-site"
    cross_origin_embedder_policy: str | None = None
    x_xss_protection: str | None = "0"
    cache_authenticated: str | None = "no-store"

    def build_headers(self, debug: bool = False) -> list[tuple[bytes, bytes]]:
        """Build pre-encoded header pairs, excluding disabled headers and CSP.

        CSP is handled separately by the middleware for per-request nonce support.
        HSTS is excluded when debug=True to avoid poisoning browsers during
        local HTTP development.
        """
        header_map = {
            "x-content-type-options": self.x_content_type_options,
            "x-frame-options": self.x_frame_options,
            "referrer-policy": self.referrer_policy,
            "permissions-policy": self.permissions_policy,
            "cross-origin-opener-policy": self.cross_origin_opener_policy,
            "cross-origin-resource-policy": self.cross_origin_resource_policy,
            "cross-origin-embedder-policy": self.cross_origin_embedder_policy,
            "x-xss-protection": self.x_xss_protection,
        }

        # HSTS only in production (not debug mode)
        if not debug:
            header_map["strict-transport-security"] = self.strict_transport_security

        return [
            (name.encode(), value.encode())
            for name, value in header_map.items()
            if value
        ]


class SessionConfig(BaseModel):
    """Session cookie configuration."""

    cookie_name: str = "session"
    cookie_domain: str | None = None  # None = exact host only
    max_age: int = 86400  # 1 day in seconds


class CSRFSettings(BaseModel):
    """CSRF protection configuration."""

    exclude: list[str] = []


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    enabled: bool = True
    requests_per_minute: int = 60
    auth_requests_per_minute: int = 10
    paths: dict[str, int] = {}  # per-path-prefix overrides, e.g. {"/api": 120}


class TrustedProxySourceConfig(BaseModel):
    """Pluggable source that publishes a list of proxy CIDRs over HTTP."""

    name: str
    url: str
    format: Literal["text", "json", "cidr-list"] = "text"
    path: str | None = None  # JSONPath-like selector for "json" format (e.g. "prefixes[*].ip_prefix")
    refresh_interval: str = "1h"  # duration string: "30m", "1h", "24h"
    fallback: str | None = None  # absolute path to a bundled CIDR file


class TrustedProxyConfig(BaseModel):
    """Trust model for resolving the client IP from XFF / proxy headers.

    The resolver treats the socket peer as ground truth and only honors
    forwarding headers when the peer sits inside the trusted set.
    """

    trusted: list[str] = []  # explicit CIDRs + bare IPs
    trust_private_networks: bool | None = None  # None = auto (True if containerized)
    client_ip_header: str = "x-forwarded-for"
    cdn_header: str | None = None  # e.g. "cf-connecting-ip"; auto-set by `cdn` preset
    cdn: str | None = None  # preset key: "cloudflare" | "fastly" | "cloudfront"
    max_hops: int = 5
    strict: bool = False  # reject unresolvable chains with 400
    explicit: bool = False  # disable all auto-detection (K8s, Docker, loopback, RFC1918)
    disabled_sources: list[str] = []  # named sources/presets to exclude
    sources: list[TrustedProxySourceConfig] = []


class RedisConfig(BaseModel):
    """Redis connection configuration (shared across features)."""

    url: str = ""
    prefix: str = ""  # e.g. "myapp" → keys like "myapp:skrift:notifications"

    def make_key(self, *parts: str) -> str:
        """Build a namespaced key.

        Example: redis.make_key("skrift", "notifications") → "myapp:skrift:notifications"
        """
        segments = [self.prefix] if self.prefix else []
        segments.extend(parts)
        return ":".join(segments)


class LogfireConfig(BaseModel):
    """Pydantic Logfire observability configuration."""

    enabled: bool = False
    service_name: str = "skrift"
    environment: str | None = None  # defaults to SKRIFT_ENV
    sample_rate: float = 1.0
    console: bool = False


class SiteConfig(BaseModel):
    """Configuration for a subdomain site."""

    subdomain: str
    controllers: list[str] = []
    theme: str = ""
    page_types: list[PageTypeConfig] = []


class APIKeyConfig(BaseModel):
    """API key authentication configuration."""

    enabled: bool = True
    default_expiration_days: int = 365
    max_keys_per_user: int = 10
    refresh_token_expiration_days: int = 30


class NotificationsConfig(BaseModel):
    """Notification backend configuration."""

    backend: str = ""  # empty = InMemoryBackend; or "module:ClassName" import string
    webhook_secret: str = ""  # empty = webhook disabled


class AuthConfig(BaseModel):
    """Authentication configuration."""

    redirect_base_url: str = "http://localhost:8000"
    allowed_redirect_domains: list[str] = []
    second_factors: SecondFactorSettings = SecondFactorSettings()
    methods: dict[str, dict] = {}
    providers: dict[str, ProviderConfig] = {}
    _provider_types: dict[str, str] = PrivateAttr(default_factory=dict)
    _method_types: dict[str, str] = PrivateAttr(default_factory=dict)
    _method_configs: dict[str, dict] = PrivateAttr(default_factory=dict)

    @classmethod
    def _resolve_provider_type(cls, key: str, config: dict) -> str:
        """Resolve the provider type from config, falling back to key."""
        return config.get("provider", "") or key

    @classmethod
    def _parse_provider(cls, name: str, config: dict) -> ProviderConfig:
        """Parse a provider config, using the appropriate model based on provider type."""
        config = dict(config)  # shallow copy
        provider_type = config.pop("provider", "") or name

        if provider_type == "dummy":
            return DummyProviderConfig(**config)
        if provider_type == "skrift":
            return SkriftProviderConfig(**config)
        return OAuthProviderConfig(**config)

    def __init__(self, **data):
        # Convert raw auth.methods/auth.providers to a unified internal model.
        method_types = {}
        method_configs = {}
        raw_methods = {}

        if isinstance(data.get("methods"), dict) or isinstance(data.get("providers"), dict):
            raw_methods = get_auth_method_configs(data)
            data["methods"] = raw_methods
            provider_dicts = get_auth_provider_configs(data)
            data["providers"] = provider_dicts

            for name, config in raw_methods.items():
                method_type = config.get("type", "") or "oauth"
                method_types[name] = method_type
                method_configs[name] = dict(config)

        # Convert raw provider dicts to appropriate config objects
        provider_types = {}
        if "providers" in data and isinstance(data["providers"], dict):
            parsed_providers = {}
            for name, config in data["providers"].items():
                if isinstance(config, dict):
                    provider_types[name] = self._resolve_provider_type(name, config)
                    parsed_providers[name] = self._parse_provider(name, config)
                else:
                    parsed_providers[name] = config
                    provider_types[name] = name
            data["providers"] = parsed_providers
        super().__init__(**data)
        self._provider_types = provider_types
        self._method_types = method_types
        self._method_configs = method_configs

    def get_provider_type(self, key: str) -> str:
        """Get the provider type for a config key. Falls back to key itself."""
        return self._provider_types.get(key, key)

    def get_primary_auth_method_type(self, key: str) -> str:
        """Map provider-oriented config to the current primary-auth method type."""
        if key in self._method_types:
            return self._method_types[key]
        provider_type = self.get_provider_type(key)
        if provider_type == "dummy":
            return "dummy"
        return "oauth"

    def get_method_config(self, key: str) -> dict:
        """Get the raw config dict for a primary auth method key."""
        return dict(self._method_configs.get(key, {}))

    def get_method_keys(self) -> list[str]:
        """Return configured auth method keys in config order."""
        if self.methods:
            return list(self.methods.keys())
        return list(self.providers.keys())

    def get_redirect_uri(self, provider: str) -> str:
        """Get the OAuth callback URL for a provider."""
        return f"{self.redirect_base_url}/auth/{provider}/callback"


class S3Config(BaseModel):
    """S3-compatible storage configuration."""

    bucket: str = ""
    region: str = "us-east-1"
    prefix: str = ""
    endpoint_url: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    acl: str = "private"
    public_url: str = ""
    presign_ttl: int = 3600


class StoreConfig(BaseModel):
    """Configuration for a single storage store."""

    backend: str = "local"
    local_path: str = "./uploads"
    max_upload_size: int = 10_485_760  # 10 MB
    s3: S3Config = S3Config()


class StorageConfig(BaseModel):
    """Top-level storage configuration with named stores."""

    default: str = "default"
    stores: dict[str, StoreConfig] = {"default": StoreConfig()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    debug: bool = False
    secret_key: str
    theme: str = ""
    domain: str = ""

    # Subdomain sites (loaded from app.yaml)
    sites: dict[str, SiteConfig] = {}

    # Database config (loaded from app.yaml)
    db: DatabaseConfig = DatabaseConfig()

    # Auth config (loaded from app.yaml)
    auth: AuthConfig = AuthConfig()

    # OAuth2 Authorization Server enabled flag
    oauth2_enabled: bool = False

    # API key configuration
    api_keys: APIKeyConfig = APIKeyConfig()

    # Session config (loaded from app.yaml)
    session: SessionConfig = SessionConfig()

    # CSRF config (loaded from app.yaml)
    csrf: CSRFSettings | None = None

    # Security headers config (loaded from app.yaml)
    security_headers: SecurityHeadersConfig = SecurityHeadersConfig()

    # Rate limit config (loaded from app.yaml)
    rate_limit: RateLimitConfig = RateLimitConfig()

    # Trusted proxy / client IP resolution (loaded from app.yaml)
    trusted_proxy: TrustedProxyConfig = TrustedProxyConfig()

    # Redis config (loaded from app.yaml)
    redis: RedisConfig = RedisConfig()

    # Notifications config (loaded from app.yaml)
    notifications: NotificationsConfig = NotificationsConfig()

    # Logfire observability config (loaded from app.yaml)
    logfire: LogfireConfig = LogfireConfig()

    # Storage config (loaded from app.yaml)
    storage: StorageConfig = StorageConfig()

    # Page types config (loaded from app.yaml)
    page_types: list[PageTypeConfig] = list(DEFAULT_PAGE_TYPES)

    # Security contact for /.well-known/security.txt (RFC 9116)
    security_contact: str = ""


def clear_settings_cache() -> None:
    """Clear the settings cache to force reload."""
    get_settings.cache_clear()


def is_config_valid() -> tuple[bool, str | None]:
    """Check if the current configuration is valid and complete.

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        config = load_raw_app_config()
        if config is None:
            return False, f"{get_config_path().name} not found"

        # Check database URL
        db_config = config.get("db", {})
        db_url = db_config.get("url")
        if not db_url:
            return False, "Database URL not configured"

        # If it's an env var reference, check if env var is set
        if isinstance(db_url, str) and db_url.startswith("$"):
            env_var = db_url[1:]
            if not os.environ.get(env_var):
                return False, f"Database environment variable ${env_var} not set"

        # Check auth methods/providers
        auth_config = config.get("auth", {})
        methods = get_auth_method_configs(auth_config)
        if not methods:
            return False, "No authentication methods configured"

        return True, None
    except Exception as e:
        return False, str(e)


@lru_cache
def get_settings() -> Settings:
    """Load settings from .env and app.yaml."""
    # Load app.yaml config
    try:
        app_config = load_app_config()
    except FileNotFoundError:
        return Settings()
    except ValueError as e:
        config_path = get_config_path()
        raise SystemExit(
            f"Failed to load {config_path.name}: {e}"
        ) from e

    # If the config file specifies an environment, propagate it so the rest
    # of the system (logfire defaults, etc.) sees the correct value.
    if "environment" in app_config:
        os.environ[SKRIFT_ENV] = app_config["environment"]

    # Build nested configs from YAML - pass directly to Settings to avoid
    # model_copy issues with nested BaseModel instances in Pydantic v2
    kwargs = {}

    if "db" in app_config:
        kwargs["db"] = DatabaseConfig(**app_config["db"])

    if "auth" in app_config:
        kwargs["auth"] = AuthConfig(**app_config["auth"])

    if "session" in app_config:
        kwargs["session"] = SessionConfig(**app_config["session"])

    if "csrf" in app_config:
        kwargs["csrf"] = CSRFSettings(**app_config["csrf"])

    if "security_headers" in app_config:
        kwargs["security_headers"] = SecurityHeadersConfig(**app_config["security_headers"])

    if "rate_limit" in app_config:
        kwargs["rate_limit"] = RateLimitConfig(**app_config["rate_limit"])

    if "trusted_proxy" in app_config:
        kwargs["trusted_proxy"] = TrustedProxyConfig(**app_config["trusted_proxy"])

    if "redis" in app_config:
        kwargs["redis"] = RedisConfig(**app_config["redis"])

    if "notifications" in app_config:
        kwargs["notifications"] = NotificationsConfig(**app_config["notifications"])

    if "logfire" in app_config:
        kwargs["logfire"] = LogfireConfig(**app_config["logfire"])

    if "oauth2_enabled" in app_config:
        kwargs["oauth2_enabled"] = app_config["oauth2_enabled"]

    if "storage" in app_config:
        storage_data = app_config["storage"]
        stores = {}
        for name, store_data in storage_data.get("stores", {}).items():
            s3_data = store_data.pop("s3", None)
            store = StoreConfig(**store_data)
            if s3_data:
                store.s3 = S3Config(**s3_data)
            stores[name] = store
        kwargs["storage"] = StorageConfig(
            default=storage_data.get("default", "default"),
            stores=stores or {"default": StoreConfig()},
        )

    if "page_types" in app_config:
        kwargs["page_types"] = [PageTypeConfig(**pt) for pt in app_config["page_types"]]

    if "theme" in app_config:
        kwargs["theme"] = app_config["theme"]

    if "domain" in app_config:
        kwargs["domain"] = app_config["domain"]

    if "security_contact" in app_config:
        kwargs["security_contact"] = app_config["security_contact"]

    if "sites" in app_config:
        kwargs["sites"] = {
            name: SiteConfig(**cfg) for name, cfg in app_config["sites"].items()
        }

    # Create Settings with YAML nested configs
    # BaseSettings will still load debug/secret_key from env, but kwargs take precedence
    return Settings(**kwargs)
