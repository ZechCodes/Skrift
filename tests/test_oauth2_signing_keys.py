"""Integration tests for the OAuth2 signing-key service against SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from joserfc.jwk import ECKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.db.base import Base
from skrift.db.models import OAuth2SigningKey  # noqa: F401  (registers table)
from skrift.db.services import oauth2_signing_key_service as signing_keys


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _all_keys(db_session) -> list[OAuth2SigningKey]:
    result = await db_session.execute(select(OAuth2SigningKey))
    return list(result.scalars().all())


class TestGetOrCreateActiveKey:
    @pytest.mark.asyncio
    async def test_generates_key_on_first_use(self, db_session):
        key = await signing_keys.get_or_create_active_key(db_session)
        assert key.is_active is True
        assert key.algorithm == "ES256"
        assert key.retired_at is None
        assert "BEGIN PRIVATE KEY" in key.private_key_pem
        assert key.kid

    @pytest.mark.asyncio
    async def test_returns_same_key_when_already_active(self, db_session):
        first = await signing_keys.get_or_create_active_key(db_session)
        second = await signing_keys.get_or_create_active_key(db_session)
        assert first.kid == second.kid
        assert len(await _all_keys(db_session)) == 1

    @pytest.mark.asyncio
    async def test_private_key_pem_is_importable(self, db_session):
        key = await signing_keys.get_or_create_active_key(db_session)
        imported = ECKey.import_key(key.private_key_pem)
        assert imported.thumbprint() == key.kid


class TestRotate:
    @pytest.mark.asyncio
    async def test_rotate_retires_previous_and_activates_new(self, db_session):
        original = await signing_keys.get_or_create_active_key(db_session)
        rotated = await signing_keys.rotate(db_session)

        assert rotated.kid != original.kid
        assert rotated.is_active is True
        assert rotated.retired_at is None

        keys_by_kid = {key.kid: key for key in await _all_keys(db_session)}
        assert keys_by_kid[original.kid].is_active is False
        assert keys_by_kid[original.kid].retired_at is not None

    @pytest.mark.asyncio
    async def test_only_one_active_key_after_rotation(self, db_session):
        await signing_keys.get_or_create_active_key(db_session)
        await signing_keys.rotate(db_session)
        active = [key for key in await _all_keys(db_session) if key.is_active]
        assert len(active) == 1


class TestListJwksKeys:
    @pytest.mark.asyncio
    async def test_lists_active_key_as_public_jwk(self, db_session):
        key = await signing_keys.get_or_create_active_key(db_session)
        jwks = await signing_keys.list_jwks_keys(db_session)
        assert len(jwks) == 1
        jwk = jwks[0]
        assert jwk["kid"] == key.kid
        assert jwk["kty"] == "EC"
        assert jwk["crv"] == "P-256"
        assert jwk["alg"] == "ES256"
        assert jwk["use"] == "sig"
        assert "d" not in jwk  # private component must not leak

    @pytest.mark.asyncio
    async def test_retired_key_still_listed_until_pruned(self, db_session):
        original = await signing_keys.get_or_create_active_key(db_session)
        rotated = await signing_keys.rotate(db_session)
        listed_kids = {jwk["kid"] for jwk in await signing_keys.list_jwks_keys(db_session)}
        assert listed_kids == {original.kid, rotated.kid}


class TestPruneExpired:
    @pytest.mark.asyncio
    async def test_keeps_recently_retired_key(self, db_session):
        await signing_keys.get_or_create_active_key(db_session)
        await signing_keys.rotate(db_session)
        now = datetime.now(tz=timezone.utc)
        deleted = await signing_keys.prune_expired(
            db_session, now=now, access_token_ttl=900, clock_skew=60
        )
        assert deleted == 0
        assert len(await _all_keys(db_session)) == 2

    @pytest.mark.asyncio
    async def test_prunes_key_retired_past_ttl_and_skew(self, db_session):
        original = await signing_keys.get_or_create_active_key(db_session)
        rotated = await signing_keys.rotate(db_session)
        future = datetime.now(tz=timezone.utc) + timedelta(seconds=901 + 61)
        deleted = await signing_keys.prune_expired(
            db_session, now=future, access_token_ttl=900, clock_skew=60
        )
        assert deleted == 1
        remaining = {key.kid for key in await _all_keys(db_session)}
        assert remaining == {rotated.kid}
        assert original.kid not in remaining

    @pytest.mark.asyncio
    async def test_never_prunes_active_key(self, db_session):
        active = await signing_keys.get_or_create_active_key(db_session)
        far_future = datetime.now(tz=timezone.utc) + timedelta(days=3650)
        deleted = await signing_keys.prune_expired(
            db_session, now=far_future, access_token_ttl=900, clock_skew=60
        )
        assert deleted == 0
        assert [key.kid for key in await _all_keys(db_session)] == [active.kid]
