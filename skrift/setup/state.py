"""Setup state detection for the Skrift setup wizard.

This module implements a two-tier detection strategy:
1. Pre-database check: Can we connect to a database?
2. Post-database check: Is setup complete (check for setup_completed_at setting)?
"""

import os
from enum import Enum
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from skrift.db.services.setting_service import SETUP_COMPLETED_AT_KEY, get_setting


class SetupStep(Enum):
    """Wizard steps."""

    DATABASE = "database"
    AUTH = "auth"
    SITE = "site"
    ADMIN = "admin"
    COMPLETE = "complete"


def app_yaml_exists() -> bool:
    """Check if app.yaml exists in the current working directory."""
    return (Path.cwd() / "app.yaml").exists()


def get_database_url_from_yaml() -> str | None:
    """Try to get the database URL from app.yaml, returning None if not configured.

    If the URL is an env var reference that isn't set, falls back to checking
    for local SQLite database files.
    """
    import yaml

    config_path = Path.cwd() / "app.yaml"
    if not config_path.exists():
        return None

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        if not config or "db" not in config:
            return None

        db_url = config["db"].get("url")
        if not db_url:
            return None

        # If it's an env var reference, try to resolve it
        if db_url.startswith("$"):
            env_var = db_url[1:]
            resolved = os.environ.get(env_var)
            if resolved:
                return resolved

            # Fallback: check for local SQLite database files
            for db_file in ["./app.db", "./data.db", "./skrift.db"]:
                if Path(db_file).exists():
                    return f"sqlite+aiosqlite:///{db_file}"

            return None

        return db_url
    except Exception:
        return None


async def can_connect_to_database() -> tuple[bool, str | None]:
    """Test if we can connect to the database.

    Returns:
        Tuple of (success, error_message)
    """
    db_url = get_database_url_from_yaml()
    if not db_url:
        return False, "Database URL not configured"

    try:
        engine = create_async_engine(db_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True, None
    except Exception as e:
        return False, str(e)


async def is_setup_complete(db_session: AsyncSession) -> bool:
    """Check if setup has been completed by looking for the setup_completed_at setting."""
    try:
        value = await get_setting(db_session, SETUP_COMPLETED_AT_KEY)
        return value is not None
    except Exception:
        # Table might not exist yet
        return False


async def get_setup_step(db_session: AsyncSession | None = None) -> SetupStep:
    """Determine which setup step the user should be on.

    Args:
        db_session: Database session if available

    Returns:
        The appropriate setup step
    """
    # Pre-database check
    if not app_yaml_exists():
        return SetupStep.DATABASE

    db_url = get_database_url_from_yaml()
    if not db_url:
        return SetupStep.DATABASE

    can_connect, _ = await can_connect_to_database()
    if not can_connect:
        return SetupStep.DATABASE

    # Post-database check - need a session
    if db_session is None:
        return SetupStep.DATABASE

    if not await is_setup_complete(db_session):
        # Check wizard progress stored in session (handled by controller)
        return SetupStep.AUTH

    return SetupStep.COMPLETE
