import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env file early so env vars are available for YAML interpolation
# Use explicit path to handle subprocess spawning (uvicorn workers)
_env_file = Path(__file__).parent.parent / ".env"
load_dotenv(_env_file)

# Pattern to match $VAR_NAME environment variable references
ENV_VAR_PATTERN = re.compile(r"\$([A-Z_][A-Z0-9_]*)")


def interpolate_env_vars(value):
    """Recursively replace $VAR_NAME with os.environ values."""
    if isinstance(value, str):

        def replace(match):
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                raise ValueError(f"Environment variable ${var} not set")
            return val

        return ENV_VAR_PATTERN.sub(replace, value)
    elif isinstance(value, dict):
        return {k: interpolate_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]
    return value


def load_app_config() -> dict:
    """Load and parse app.yaml with environment variable interpolation."""
    config_path = Path.cwd() / "app.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"app.yaml not found at {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    return interpolate_env_vars(config)


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


class AuthConfig(BaseModel):
    """Authentication configuration."""

    redirect_base_url: str = "http://localhost:8000"
    providers: dict[str, OAuthProviderConfig] = {}

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

    # Merge YAML config with settings
    updates = {}

    if "db" in app_config:
        updates["db"] = DatabaseConfig(**app_config["db"])

    if "auth" in app_config:
        updates["auth"] = AuthConfig(**app_config["auth"])

    if updates:
        return base_settings.model_copy(update=updates)

    return base_settings
