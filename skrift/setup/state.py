"""Setup state detection for the Skrift setup wizard.

This module implements a two-tier detection strategy:
1. Pre-database check: Can we connect to a database?
2. Post-database check: Is setup complete (check for setup_completed_at setting)?

Smart step detection: If config is already present, skip to the first incomplete step.
"""

import logging
import os
import subprocess
from enum import Enum
from pathlib import Path
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

logger = logging.getLogger(__name__)

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
    SITE_THEME_KEY,
    get_setting,
)


class SetupStep(Enum):
    """Wizard steps."""

    DATABASE = "database"
    AUTH = "auth"
    SITE = "site"
    THEME = "theme"
    ADMIN = "admin"
    COMPLETE = "complete"


def app_yaml_exists() -> bool:
    """Check if app.yaml exists in the current working directory."""
    return get_config_path().exists()


def _load_db_config_from_yaml() -> dict | None:
    """Load the db section from app.yaml, returning None if not available."""
    config_path = get_config_path()
    if not config_path.exists():
        return None

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        if not config or "db" not in config:
            return None

        return config["db"]
    except Exception:
        return None


def get_database_url_from_yaml() -> str | None:
    """Try to get the database URL from app.yaml, returning None if not configured.

    If the URL is an env var reference that isn't set, falls back to checking
    for local SQLite database files.
    """
    db_config = _load_db_config_from_yaml()
    if not db_config:
        return None

    db_url = db_config.get("url")
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


def get_database_schema_from_yaml() -> str | None:
    """Get the database schema from app.yaml, returning None if not configured."""
    db_config = _load_db_config_from_yaml()
    if not db_config:
        return None

    schema = db_config.get("schema")
    if not schema:
        return None

    # Resolve env var references
    if isinstance(schema, str) and schema.startswith("$"):
        return os.environ.get(schema[1:])

    return schema


def create_setup_engine(db_url: str):
    """Create an async engine with schema configuration applied.

    This mirrors the schema setup from the main app's create_app() to ensure
    setup operations target the correct database schema.
    """
    from skrift.db.base import Base

    schema = get_database_schema_from_yaml()
    kwargs: dict = {}

    if schema and "sqlite" not in db_url:
        Base.metadata.schema = schema
        kwargs["execution_options"] = {
            "schema_translate_map": {None: schema},
        }

    return create_async_engine(db_url, **kwargs)


async def can_connect_to_database() -> tuple[bool, str | None]:
    """Test if we can connect to the database.

    Returns:
        Tuple of (success, error_message)
    """
    db_url = get_database_url_from_yaml()
    if not db_url:
        return False, "Database URL not configured"

    try:
        engine = create_setup_engine(db_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True, None
    except Exception as e:
        logger.error("Database connection failed: %s", e, exc_info=True)
        return False, str(e)


async def is_setup_complete(db_session: AsyncSession) -> bool:
    """Check if setup has been completed by looking for the setup_completed_at setting."""
    try:
        value = await get_setting(db_session, SETUP_COMPLETED_AT_KEY)
        return value is not None
    except Exception:
        # Table might not exist yet (pre-migration)
        logger.debug("Could not check setup_completed_at (table may not exist yet)", exc_info=True)
        return False


def is_auth_configured() -> bool:
    """Check if at least one auth provider is configured in app.yaml.

    The dummy provider is considered configured without credentials.
    OAuth providers require both client_id and client_secret.

    Returns:
        True if at least one provider is configured, False otherwise.
    """
    from skrift.setup.providers import DUMMY_PROVIDER_KEY

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

        for provider_name, provider_config in providers.items():
            if provider_name == DUMMY_PROVIDER_KEY:
                return True

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
        logger.info("Running migrations: skrift db upgrade heads")
        result = subprocess.run(
            ["skrift", "db", "upgrade", "heads"],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
            timeout=60,
        )
        if result.returncode == 0:
            _migrations_run = True
            logger.info("Migrations completed successfully via skrift db")
            return True, None
        # If skrift db fails, log and try alembic directly
        logger.warning(
            "skrift db upgrade failed (exit code %d), falling back to alembic. stdout=%s stderr=%s",
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )
    except subprocess.TimeoutExpired:
        logger.error("skrift db upgrade timed out after 60s, falling back to alembic")
    except FileNotFoundError:
        logger.debug("skrift command not found, falling back to alembic")

    try:
        logger.info("Running migrations: alembic upgrade heads")
        result = subprocess.run(
            ["alembic", "upgrade", "heads"],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
            timeout=60,
        )
        if result.returncode == 0:
            _migrations_run = True
            logger.info("Migrations completed successfully via alembic")
            return True, None
        error_msg = result.stderr.strip()
        logger.error(
            "alembic upgrade failed (exit code %d). stdout=%s stderr=%s",
            result.returncode,
            result.stdout.strip(),
            error_msg,
        )
        return False, error_msg
    except subprocess.TimeoutExpired:
        logger.error("alembic upgrade timed out after 60s")
        return False, "Migration timed out"
    except FileNotFoundError:
        logger.error("Neither skrift nor alembic commands found on PATH")
        return False, "skrift db command not found"
    except Exception as e:
        logger.error("Unexpected migration error: %s", e, exc_info=True)
        return False, str(e)


async def _is_setting_configured(setting_key: str) -> bool:
    """Check if a setting exists in the database.

    Creates a temporary engine/session for the check. Returns False if the
    database isn't reachable or the settings table doesn't exist yet.
    """
    db_url = get_database_url_from_yaml()
    if not db_url:
        return False

    engine = None
    try:
        engine = create_setup_engine(db_url)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async_session = async_sessionmaker(engine, expire_on_commit=False)
        async with async_session() as session:
            try:
                value = await get_setting(session, setting_key)
                return value is not None
            except Exception:
                logger.debug("Could not check %s setting (table may not exist yet)", setting_key, exc_info=True)
                return False
    except Exception:
        logger.debug("Could not connect to check %s", setting_key, exc_info=True)
        return False
    finally:
        if engine:
            await engine.dispose()


async def is_site_configured() -> bool:
    """Check if site settings have been configured (site_name is set)."""
    return await _is_setting_configured(SITE_NAME_KEY)


async def is_theme_configured() -> bool:
    """Check if the theme step has been completed (site_theme key exists)."""
    return await _is_setting_configured(SITE_THEME_KEY)


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
    migration_success, migration_error = run_migrations_if_needed()
    if not migration_success:
        logger.error("Setup migrations failed during step detection: %s", migration_error)
        # If migrations fail, go back to database step to show the error
        return SetupStep.DATABASE

    # Step 2: Auth - check if at least one provider is configured
    if not is_auth_configured():
        return SetupStep.AUTH

    # Step 3: Site - check if site settings exist in DB
    if not await is_site_configured():
        return SetupStep.SITE

    # Step 4: Theme - only when themes/ directory exists with valid themes
    from skrift.lib.theme import themes_available
    if themes_available() and not await is_theme_configured():
        return SetupStep.THEME

    # Step 5 (or 4 without themes): Admin - always go here if setup not complete
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
