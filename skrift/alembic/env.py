"""Alembic environment configuration for async SQLAlchemy migrations."""

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from skrift.config import get_settings
from skrift.db.base import Base

# Import all models to ensure they're registered with Base.metadata
from skrift.db.models.notification import StoredNotification  # noqa: F401
from skrift.db.models.oauth_account import OAuthAccount  # noqa: F401
from skrift.db.models.user import User  # noqa: F401
from skrift.db.models.page import Page  # noqa: F401
from skrift.db.models.role import Role, RolePermission  # noqa: F401

# Dynamically import user model modules from app.yaml
import importlib
import os
import sys

from skrift.config import load_model_modules

# Ensure project cwd is on sys.path for user module imports
_cwd = os.getcwd()
if _cwd not in sys.path:
    sys.path.insert(0, _cwd)

for _module_path in load_model_modules():
    try:
        importlib.import_module(_module_path)
    except ImportError:
        pass  # Graceful fallback if models can't be loaded

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for 'autogenerate' support
target_metadata = Base.metadata


def get_url() -> str:
    """Get database URL from settings or alembic.ini."""
    try:
        settings = get_settings()
        return settings.db.url
    except Exception:
        # Fall back to alembic.ini config if settings can't be loaded
        return config.get_main_option("sqlalchemy.url", "")


def get_schema() -> str | None:
    """Get database schema from settings, if configured."""
    try:
        settings = get_settings()
        return settings.db.db_schema
    except Exception:
        return None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine.
    Calls to context.execute() emit the SQL to the script output.
    """
    url = get_url()
    schema = get_schema()

    if schema:
        Base.metadata.schema = schema

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=bool(schema),
        version_table_schema=schema,
    )

    with context.begin_transaction():
        if schema:
            context.execute(f"SET search_path TO {schema}")
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations within a connection context."""
    schema = get_schema()

    if schema:
        Base.metadata.schema = schema

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=bool(schema),
        version_table_schema=schema,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        schema = get_schema()
        if schema:
            await connection.execute(sa.text(f"SET search_path TO {schema}"))
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
