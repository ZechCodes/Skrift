from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    debug: bool = False
    secret_key: str

    # Database
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # Google OAuth
    google_client_id: str
    google_client_secret: str
    oauth_redirect_base_url: str = "http://localhost:8000"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.oauth_redirect_base_url}/auth/google/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
