import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
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


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    url: str = "sqlite+aiosqlite:///./app.db"
    pool_size: int = 5
    pool_overflow: int = 10
    pool_timeout: int = 30
    echo: bool = False


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


class AuthConfig(BaseModel):
    """Authentication configuration."""

    redirect_base_url: str = "http://localhost:8000"
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

    # Database config (loaded from app.yaml)
    db: DatabaseConfig = DatabaseConfig()

    # Auth config (loaded from app.yaml)
    auth: AuthConfig = AuthConfig()


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
    # First create base settings from .env
    base_settings = Settings()

    # Load app.yaml config
    try:
        app_config = load_app_config()
    except FileNotFoundError:
        return base_settings
    except ValueError:
        # Missing environment variables - return base settings
        return base_settings

    # Merge YAML config with settings
    updates = {}

    if "db" in app_config:
        updates["db"] = DatabaseConfig(**app_config["db"])

    if "auth" in app_config:
        updates["auth"] = AuthConfig(**app_config["auth"])

    if updates:
        return base_settings.model_copy(update=updates)

    return base_settings
