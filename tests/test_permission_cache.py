"""Tests for the bounded permission cache in ``skrift.auth.services``.

The cache is process-global, so every test starts from a cleared cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from skrift.auth import services
from skrift.auth.services import (
    get_user_permissions,
    invalidate_user_permissions_cache,
)


class _StubRolePermission:
    def __init__(self, permission: str) -> None:
        self.permission = permission


class _StubRole:
    def __init__(self, name: str, permissions: list[str]) -> None:
        self.name = name
        self.permissions = [_StubRolePermission(p) for p in permissions]


class _StubUser:
    def __init__(self, roles: list[_StubRole]) -> None:
        self.roles = roles


class _StubResult:
    def __init__(self, user: _StubUser | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> _StubUser | None:
        return self._user


class _CountingSession:
    """AsyncSession stand-in that counts how many queries it served."""

    def __init__(self, user: _StubUser | None = None) -> None:
        self.user = user
        self.query_count = 0

    async def execute(self, statement) -> _StubResult:
        self.query_count += 1
        return _StubResult(self.user)


@pytest.fixture(autouse=True)
def clear_cache():
    invalidate_user_permissions_cache()
    yield
    invalidate_user_permissions_cache()


class TestPermissionCacheBehavior:
    async def test_fresh_entry_is_served_from_cache(self):
        session = _CountingSession(_StubUser([_StubRole("editor", ["manage-pages"])]))
        user_id = str(uuid4())

        first = await get_user_permissions(session, user_id)
        second = await get_user_permissions(session, user_id)

        assert session.query_count == 1
        assert first is second
        assert first.roles == {"editor"}
        assert first.permissions == {"manage-pages"}

    async def test_expired_entry_is_requeried(self):
        session = _CountingSession(_StubUser([_StubRole("editor", ["manage-pages"])]))
        user_id = str(uuid4())

        await get_user_permissions(session, user_id)
        stale_time = datetime.now() - services.CACHE_TTL - timedelta(seconds=1)
        _, cached_permissions = services._permission_cache[user_id]
        services._permission_cache[user_id] = (stale_time, cached_permissions)

        await get_user_permissions(session, user_id)

        assert session.query_count == 2

    async def test_invalidating_one_user_leaves_others_cached(self):
        session = _CountingSession(_StubUser([]))
        kept_user_id = str(uuid4())
        dropped_user_id = str(uuid4())

        await get_user_permissions(session, kept_user_id)
        await get_user_permissions(session, dropped_user_id)
        invalidate_user_permissions_cache(dropped_user_id)

        assert kept_user_id in services._permission_cache
        assert dropped_user_id not in services._permission_cache

    async def test_invalidating_everything_clears_the_cache(self):
        session = _CountingSession(_StubUser([]))
        await get_user_permissions(session, str(uuid4()))
        await get_user_permissions(session, str(uuid4()))

        invalidate_user_permissions_cache()

        assert len(services._permission_cache) == 0


class TestPermissionCacheBounds:
    async def test_cache_never_exceeds_max_entries(self, monkeypatch):
        monkeypatch.setattr(services, "MAX_PERMISSION_CACHE_ENTRIES", 5)
        session = _CountingSession(_StubUser([]))

        for _ in range(50):
            await get_user_permissions(session, str(uuid4()))

        assert len(services._permission_cache) == 5

    async def test_least_recently_used_entry_is_evicted_first(self, monkeypatch):
        monkeypatch.setattr(services, "MAX_PERMISSION_CACHE_ENTRIES", 3)
        session = _CountingSession(_StubUser([]))
        first_user_id = str(uuid4())
        second_user_id = str(uuid4())
        third_user_id = str(uuid4())

        await get_user_permissions(session, first_user_id)
        await get_user_permissions(session, second_user_id)
        await get_user_permissions(session, third_user_id)
        # Touching the first user makes the second the least recently used.
        await get_user_permissions(session, first_user_id)
        await get_user_permissions(session, str(uuid4()))

        assert first_user_id in services._permission_cache
        assert second_user_id not in services._permission_cache
        assert third_user_id in services._permission_cache

    async def test_expired_entries_are_purged_on_write(self):
        session = _CountingSession(_StubUser([]))
        abandoned_user_ids = [str(uuid4()) for _ in range(10)]
        for user_id in abandoned_user_ids:
            await get_user_permissions(session, user_id)

        stale_time = datetime.now() - services.CACHE_TTL - timedelta(seconds=1)
        for user_id in abandoned_user_ids:
            _, cached_permissions = services._permission_cache[user_id]
            services._permission_cache[user_id] = (stale_time, cached_permissions)

        await get_user_permissions(session, str(uuid4()))

        assert len(services._permission_cache) == 1
