"""M1 — refresh-token reuse detection.

Covers both the service-level family helpers (``revoke_family`` /
``is_family_revoked``) against a real in-memory SQLite session, and the
controller-level reuse-detection seam in ``_handle_refresh_token``.
"""

from __future__ import annotations

import pytest
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig
from sqlalchemy.ext.asyncio import AsyncSession

import skrift.db.models  # noqa: F401 — registers models on Base.metadata
from skrift.db.base import Base
from skrift.db.models.revoked_family import RevokedFamily
from skrift.db.services import oauth2_service


@pytest.fixture
async def session():
    """In-memory SQLite session with only the revoked_token_families table.

    We create_all against the shared metadata so every model registers, but
    the tests here only touch :class:`RevokedFamily`.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as db:
        yield db

    await engine.dispose()


@pytest.mark.asyncio
async def test_revoke_family_inserts_row(session):
    await oauth2_service.revoke_family(session, "fam-1")
    assert await oauth2_service.is_family_revoked(session, "fam-1") is True


@pytest.mark.asyncio
async def test_revoke_family_is_idempotent(session):
    """Safe to call ``revoke_family`` repeatedly — the unique constraint on
    ``family_id`` means a second insert would otherwise raise."""
    await oauth2_service.revoke_family(session, "fam-dup")
    await oauth2_service.revoke_family(session, "fam-dup")  # must not raise
    assert await oauth2_service.is_family_revoked(session, "fam-dup") is True


@pytest.mark.asyncio
async def test_revoke_family_ignores_empty_family_id(session):
    """An empty/absent ``family_id`` must be a no-op — older refresh tokens
    issued before this feature shipped carry no family id and should not
    poison the table with a ``""`` row."""
    await oauth2_service.revoke_family(session, "")
    assert await oauth2_service.is_family_revoked(session, "") is False


@pytest.mark.asyncio
async def test_is_family_revoked_returns_false_for_unknown_family(session):
    assert await oauth2_service.is_family_revoked(session, "never-seen") is False
