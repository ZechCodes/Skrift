"""Setup state detection for the Skrift setup wizard.

This module implements a two-tier detection strategy:
1. Pre-database check: Can we connect to a database?
2. Post-database check: Is setup complete (check for setup_completed_at setting)?

Smart step detection: If config is already present, skip to the first incomplete step.
"""

import os
import subprocess
from enum import Enum
from pathlib import Path
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# Track if migrations have been run this session to avoid running multiple times
_migrations_run = False


def reset_migrations_flag() -> None:
    """Reset the migrations flag to allow re-running migrations.

    Call this when starting the configuring page to ensure migrations run fresh.
    """
    global _migrations_run
    _migrations_run = False

from skrift.config import get_config_path
from skrift.db.services.setting_service import (
    SETUP_COMPLETED_AT_KEY,
    SITE_NAME_KEY,
    get_setting,
)


class SetupStep(Enum):
    """Wizard steps."""

    DATABASE = "database"
    AUTH = "auth"
    SITE = "site"
    ADMIN = "admin"
    COMPLETE = "complete"


def app_yaml_exists() -> bool:
    """Check if app.yaml exists in the current working directory."""
    return get_config_path().exists()


def get_database_url_from_yaml() -> str | None:
    """Try to get the database URL from app.yaml, returning None if not configured.

    If the URL is an env var reference that isn't set, falls back to checking
    for local SQLite database files.
    """
    import yaml

    config_path = get_config_path()
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


def is_auth_configured() -> bool:
    """Check if at least one OAuth provider is fully configured in app.yaml.

    A provider is considered configured if it has both client_id and client_secret.

    Returns:
        True if at least one provider is configured, False otherwise.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return False

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        if not config:
            return False

        auth = config.get("auth", {})
        providers = auth.get("providers", {})

        for _, provider_config in providers.items():
            if not isinstance(provider_config, dict):
                continue
            # Check if provider has both client_id and client_secret (even as env var refs)
            client_id = provider_config.get("client_id", "")
            client_secret = provider_config.get("client_secret", "")
            if client_id and client_secret:
                return True

        return False
    except Exception:
        return False


def run_migrations_if_needed() -> tuple[bool, str | None]:
    """Run database migrations if they haven't been run this session.

    This ensures the database schema is up to date before checking for
    settings or other database-dependent configuration.

    Returns:
        Tuple of (success, error_message)
    """
    global _migrations_run
    if _migrations_run:
        return True, None

    try:
        # Try skrift db first (the correct command)
        result = subprocess.run(
            ["skrift", "db", "upgrade", "heads"],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
            timeout=60,
        )
        if result.returncode == 0:
            _migrations_run = True
            return True, None
        # If skrift db fails, try alembic directly
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        result = subprocess.run(
            ["alembic", "upgrade", "heads"],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
            timeout=60,
        )
        if result.returncode == 0:
            _migrations_run = True
            return True, None
        return False, result.stderr
    except subprocess.TimeoutExpired:
        return False, "Migration timed out"
    except FileNotFoundError:
        return False, "skrift db command not found"
    except Exception as e:
        return False, str(e)


async def is_site_configured() -> bool:
    """Check if site settings have been configured in the database.

    The site step is considered complete if site_name has been set.
    Returns False if the settings table doesn't exist yet (pre-migration).

    Returns:
        True if site is configured, False otherwise.
    """
    db_url = get_database_url_from_yaml()
    if not db_url:
        return False

    engine = None
    try:
        engine = create_async_engine(db_url)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async_session = async_sessionmaker(engine, expire_on_commit=False)
        async with async_session() as session:
            try:
                site_name = await get_setting(session, SITE_NAME_KEY)
                return site_name is not None
            except Exception:
                # Table might not exist yet (before migration)
                return False
    except Exception:
        return False
    finally:
        if engine:
            await engine.dispose()


async def get_first_incomplete_step() -> SetupStep:
    """Determine the first incomplete step in the setup wizard.

    This function checks configuration completeness for each step and returns
    the first step that needs to be completed. Use this to skip already-configured
    steps when the user is forced back into the setup wizard.

    If database is configured and connectable, runs migrations to ensure
    all tables exist before checking database-dependent configuration.

    Returns:
        The first setup step that needs user input.
    """
    # Step 1: Database - check if we can connect
    if not app_yaml_exists():
        return SetupStep.DATABASE

    db_url = get_database_url_from_yaml()
    if not db_url:
        return SetupStep.DATABASE

    can_connect, _ = await can_connect_to_database()
    if not can_connect:
        return SetupStep.DATABASE

    # Database is configured and connectable - run migrations to ensure tables exist
    migration_success, _ = run_migrations_if_needed()
    if not migration_success:
        # If migrations fail, go back to database step to show the error
        return SetupStep.DATABASE

    # Step 2: Auth - check if at least one provider is configured
    if not is_auth_configured():
        return SetupStep.AUTH

    # Step 3: Site - check if site settings exist in DB
    if not await is_site_configured():
        return SetupStep.SITE

    # Step 4: Admin - always go here if setup not complete
    return SetupStep.ADMIN


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
