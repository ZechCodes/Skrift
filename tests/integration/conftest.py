"""Fixtures for notification backend integration tests.

Requires PostgreSQL and Redis services (see compose.yml).
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from skrift.config import DatabaseConfig, RedisConfig, Settings
from skrift.db.base import Base
from skrift.lib.notification_backends import PgNotifyBackend, RedisBackend
from skrift.lib.notifications import NotificationService


# ---------------------------------------------------------------------------
# URL fixtures (session scope, synchronous)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_url():
    return os.environ.get(
        "INTEGRATION_PG_URL",
        "postgresql+asyncpg://skrift_test:skrift_test@localhost:15432/skrift_test",
    )


@pytest.fixture(scope="session")
def redis_url():
    return os.environ.get(
        "INTEGRATION_REDIS_URL",
        "redis://localhost:16379",
    )


# ---------------------------------------------------------------------------
# Table creation (session scope, synchronous — uses asyncio.run internally)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _create_tables(pg_url):
    import skrift.db.models  # noqa: F401 — register all models on Base

    async def _setup():
        engine = create_async_engine(pg_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


# ---------------------------------------------------------------------------
# Async engine & session maker (function scope)
# ---------------------------------------------------------------------------

@pytest.fixture
async def pg_engine(pg_url, _create_tables):
    engine = create_async_engine(pg_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_maker(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Per-test table cleanup (autouse)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def clean_tables(pg_session_maker):
    async with pg_session_maker() as session:
        await session.execute(text("DELETE FROM stored_notifications"))
        await session.commit()


# ---------------------------------------------------------------------------
# Settings factories
# ---------------------------------------------------------------------------

@pytest.fixture
def settings_for_redis(pg_url, redis_url):
    return Settings(
        secret_key="test-secret",
        db=DatabaseConfig(url=pg_url),
        redis=RedisConfig(url=redis_url),
    )


@pytest.fixture
def settings_for_pg(pg_url):
    return Settings(
        secret_key="test-secret",
        db=DatabaseConfig(url=pg_url),
    )


# ---------------------------------------------------------------------------
# Backend pair helpers
# ---------------------------------------------------------------------------

async def _make_pair(backend_cls, *, settings, session_maker):
    """Create two independent (NotificationService, backend) pairs."""
    pairs = []
    for _ in range(2):
        svc = NotificationService()
        backend = backend_cls(settings=settings, session_maker=session_maker)
        svc.set_backend(backend)
        await backend.start()
        pairs.append((svc, backend))
    # Allow pub/sub subscriptions to establish
    await asyncio.sleep(0.1)
    return pairs


@pytest.fixture
async def redis_backend_pair(settings_for_redis, pg_session_maker):
    pairs = await _make_pair(
        RedisBackend, settings=settings_for_redis, session_maker=pg_session_maker,
    )
    yield pairs
    for _, backend in pairs:
        await backend.stop()


@pytest.fixture
async def pg_backend_pair(settings_for_pg, pg_session_maker):
    pairs = await _make_pair(
        PgNotifyBackend, settings=settings_for_pg, session_maker=pg_session_maker,
    )
    yield pairs
    for _, backend in pairs:
        await backend.stop()


# ---------------------------------------------------------------------------
# Parametrized meta-fixture: runs each test against both backend types
# ---------------------------------------------------------------------------

@pytest.fixture(params=["redis_backend_pair", "pg_backend_pair"])
def backend_pair(request):
    return request.getfixturevalue(request.param)
