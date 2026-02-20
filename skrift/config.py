import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
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


def get_environment() -> str:
    """Get the current environment name, normalized to lowercase.

    Reads from SKRIFT_ENV environment variable. Defaults to "production".
    """
    env = os.environ.get(SKRIFT_ENV, DEFAULT_ENVIRONMENT)
    return env.lower().strip()


def get_config_path() -> Path:
    """Get the path to the environment-specific config file.

    Production -> app.yaml
    Other envs -> app.{env}.yaml (e.g., app.dev.yaml)
    """
    env = get_environment()
    if env == "production":
        return Path.cwd() / "app.yaml"
    return Path.cwd() / f"app.{env}.yaml"


def interpolate_env_vars(value, strict: bool = True):
    """Recursively replace $VAR_NAME with os.environ values.

    Args:
        value: The value to interpolate
        strict: If True, raise an error when env var is not set.
                If False, return the original $VAR_NAME reference.
    """
    if isinstance(value, str):

        def replace(match):
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                if strict:
                    raise ValueError(f"Environment variable ${var} not set")
                return match.group(0)  # Return original $VAR_NAME
            return val

        return ENV_VAR_PATTERN.sub(replace, value)
    elif isinstance(value, dict):
        return {k: interpolate_env_vars(v, strict) for k, v in value.items()}
    elif isinstance(value, list):
        return [interpolate_env_vars(item, strict) for item in value]
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


# Union type for provider configs - dummy has no required fields
ProviderConfig = OAuthProviderConfig | DummyProviderConfig


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


class NotificationsConfig(BaseModel):
    """Notification backend configuration."""

    backend: str = ""  # empty = InMemoryBackend; or "module:ClassName" import string
    webhook_secret: str = ""  # empty = webhook disabled


class AuthConfig(BaseModel):
    """Authentication configuration."""

    redirect_base_url: str = "http://localhost:8000"
    allowed_redirect_domains: list[str] = []
    providers: dict[str, ProviderConfig] = {}

    @classmethod
    def _parse_provider(cls, name: str, config: dict) -> ProviderConfig:
        """Parse a provider config, using the appropriate model based on provider name."""
        if name == "dummy":
            return DummyProviderConfig(**config)
        return OAuthProviderConfig(**config)

    def __init__(self, **data):
        # Convert raw provider dicts to appropriate config objects
        if "providers" in data and isinstance(data["providers"], dict):
            parsed_providers = {}
            for name, config in data["providers"].items():
                if isinstance(config, dict):
                    parsed_providers[name] = self._parse_provider(name, config)
                else:
                    parsed_providers[name] = config
            data["providers"] = parsed_providers
        super().__init__(**data)

    def get_redirect_uri(self, provider: str) -> str:
        """Get the OAuth callback URL for a provider."""
        return f"{self.redirect_base_url}/auth/{provider}/callback"


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

    # Session config (loaded from app.yaml)
    session: SessionConfig = SessionConfig()

    # CSRF config (loaded from app.yaml)
    csrf: CSRFSettings | None = None

    # Security headers config (loaded from app.yaml)
    security_headers: SecurityHeadersConfig = SecurityHeadersConfig()

    # Rate limit config (loaded from app.yaml)
    rate_limit: RateLimitConfig = RateLimitConfig()

    # Redis config (loaded from app.yaml)
    redis: RedisConfig = RedisConfig()

    # Notifications config (loaded from app.yaml)
    notifications: NotificationsConfig = NotificationsConfig()

    # Logfire observability config (loaded from app.yaml)
    logfire: LogfireConfig = LogfireConfig()

    # Page types config (loaded from app.yaml)
    page_types: list[PageTypeConfig] = list(DEFAULT_PAGE_TYPES)


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

        # Check auth providers
        auth_config = config.get("auth", {})
        providers = auth_config.get("providers", {})
        if not providers:
            return False, "No authentication providers configured"

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
    except ValueError:
        # Missing environment variables - return base settings
        return Settings()

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

    if "redis" in app_config:
        kwargs["redis"] = RedisConfig(**app_config["redis"])

    if "notifications" in app_config:
        kwargs["notifications"] = NotificationsConfig(**app_config["notifications"])

    if "logfire" in app_config:
        kwargs["logfire"] = LogfireConfig(**app_config["logfire"])

    if "page_types" in app_config:
        kwargs["page_types"] = [PageTypeConfig(**pt) for pt in app_config["page_types"]]

    if "theme" in app_config:
        kwargs["theme"] = app_config["theme"]

    if "domain" in app_config:
        kwargs["domain"] = app_config["domain"]

    if "sites" in app_config:
        kwargs["sites"] = {
            name: SiteConfig(**cfg) for name, cfg in app_config["sites"].items()
        }

    # Create Settings with YAML nested configs
    # BaseSettings will still load debug/secret_key from env, but kwargs take precedence
    return Settings(**kwargs)
