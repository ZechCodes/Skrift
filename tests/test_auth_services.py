"""Tests for authentication and authorization services."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from skrift.db.models.role import Role, RolePermission

from skrift.auth.services import (
    CACHE_TTL,
    UserPermissions,
    _permission_cache,
    _permission_cache_lock,
    assign_role_to_user,
    get_user_permissions,
    invalidate_user_permissions_cache,
    remove_role_from_user,
    sync_roles_to_database,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_role_permission(permission: str) -> MagicMock:
    """Create a mock RolePermission with the given permission string."""
    rp = MagicMock()
    rp.permission = permission
    return rp


def _make_role(name: str, permissions: list[str] | None = None) -> MagicMock:
    """Create a mock Role with name and optional permissions."""
    role = MagicMock()
    role.name = name
    role.permissions = [_make_role_permission(p) for p in (permissions or [])]
    return role


def _make_user(user_id: UUID | None = None, roles: list[MagicMock] | None = None) -> MagicMock:
    """Create a mock User with id and optional roles."""
    user = MagicMock()
    user.id = user_id or uuid4()
    user.roles = list(roles) if roles else []
    return user


def _make_session_with_execute(*results) -> AsyncMock:
    """Create a mock AsyncSession whose .execute() returns successive results.

    Each element of *results* should be the value that
    ``result.scalar_one_or_none()`` returns for the corresponding
    ``session.execute()`` call.
    """
    session = AsyncMock()
    # session.add is synchronous in SQLAlchemy, so use MagicMock
    session.add = MagicMock()
    mock_results = []
    for value in results:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = value
        mock_results.append(mock_result)
    session.execute = AsyncMock(side_effect=mock_results)
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_permission_cache():
    """Ensure the permission cache is empty before and after every test."""
    _permission_cache.clear()
    yield
    _permission_cache.clear()


# ---------------------------------------------------------------------------
# _permission_cache_lock type
# ---------------------------------------------------------------------------


class TestPermissionCacheLock:
    """Verify the module-level lock is an asyncio.Lock."""

    def test_lock_is_asyncio_lock(self):
        assert isinstance(_permission_cache_lock, asyncio.Lock)


# ---------------------------------------------------------------------------
# get_user_permissions
# ---------------------------------------------------------------------------


class TestGetUserPermissions:
    """Tests for get_user_permissions()."""

    async def test_cache_miss_queries_database(self):
        """On a cache miss the database is queried and result is cached."""
        user_id = uuid4()
        role = _make_role("editor", ["manage-pages", "view-drafts"])
        user = _make_user(user_id, roles=[role])

        session = _make_session_with_execute(user)

        perms = await get_user_permissions(session, user_id)

        assert perms.user_id == str(user_id)
        assert perms.roles == {"editor"}
        assert perms.permissions == {"manage-pages", "view-drafts"}
        session.execute.assert_awaited_once()

        # Verify result is now cached
        assert str(user_id) in _permission_cache

    async def test_cache_hit_skips_database(self):
        """When the cache has a fresh entry the database is not queried."""
        user_id = uuid4()
        cached_perms = UserPermissions(
            user_id=str(user_id),
            roles={"admin"},
            permissions={"administrator"},
        )
        _permission_cache[str(user_id)] = (datetime.now(), cached_perms)

        session = AsyncMock()

        perms = await get_user_permissions(session, user_id)

        assert perms is cached_perms
        session.execute.assert_not_awaited()

    async def test_cache_ttl_expiry_re_queries(self):
        """An expired cache entry causes a fresh database query."""
        user_id = uuid4()
        stale_perms = UserPermissions(
            user_id=str(user_id),
            roles={"old_role"},
            permissions={"old_perm"},
        )
        expired_time = datetime.now() - CACHE_TTL - timedelta(seconds=1)
        _permission_cache[str(user_id)] = (expired_time, stale_perms)

        role = _make_role("author", ["view-drafts"])
        user = _make_user(user_id, roles=[role])
        session = _make_session_with_execute(user)

        perms = await get_user_permissions(session, user_id)

        assert perms.roles == {"author"}
        assert perms.permissions == {"view-drafts"}
        session.execute.assert_awaited_once()

    async def test_user_with_multiple_roles(self):
        """A user with multiple roles aggregates all permissions."""
        user_id = uuid4()
        admin_role = _make_role("admin", ["administrator", "manage-users"])
        editor_role = _make_role("editor", ["manage-pages", "view-drafts"])
        user = _make_user(user_id, roles=[admin_role, editor_role])

        session = _make_session_with_execute(user)

        perms = await get_user_permissions(session, user_id)

        assert perms.roles == {"admin", "editor"}
        assert perms.permissions == {
            "administrator",
            "manage-users",
            "manage-pages",
            "view-drafts",
        }

    async def test_user_not_found_returns_empty_permissions(self):
        """When the user does not exist an empty UserPermissions is returned."""
        user_id = uuid4()
        session = _make_session_with_execute(None)

        perms = await get_user_permissions(session, user_id)

        assert perms.user_id == str(user_id)
        assert perms.roles == set()
        assert perms.permissions == set()

    async def test_accepts_string_user_id(self):
        """get_user_permissions accepts a string user_id."""
        user_id = uuid4()
        user = _make_user(user_id, roles=[])
        session = _make_session_with_execute(user)

        perms = await get_user_permissions(session, str(user_id))

        assert perms.user_id == str(user_id)

    async def test_overlapping_permissions_deduplicated(self):
        """When two roles grant the same permission it appears only once."""
        user_id = uuid4()
        role_a = _make_role("editor", ["view-drafts", "manage-pages"])
        role_b = _make_role("moderator", ["view-drafts"])
        user = _make_user(user_id, roles=[role_a, role_b])

        session = _make_session_with_execute(user)

        perms = await get_user_permissions(session, user_id)

        assert perms.permissions == {"view-drafts", "manage-pages"}


# ---------------------------------------------------------------------------
# invalidate_user_permissions_cache
# ---------------------------------------------------------------------------


class TestInvalidateUserPermissionsCache:
    """Tests for invalidate_user_permissions_cache()."""

    def test_invalidate_single_user(self):
        """Passing a user_id removes only that user's cache entry."""
        uid1 = str(uuid4())
        uid2 = str(uuid4())
        _permission_cache[uid1] = (datetime.now(), UserPermissions(user_id=uid1))
        _permission_cache[uid2] = (datetime.now(), UserPermissions(user_id=uid2))

        invalidate_user_permissions_cache(uid1)

        assert uid1 not in _permission_cache
        assert uid2 in _permission_cache

    def test_invalidate_all_users(self):
        """Passing None clears the entire cache."""
        uid1 = str(uuid4())
        uid2 = str(uuid4())
        _permission_cache[uid1] = (datetime.now(), UserPermissions(user_id=uid1))
        _permission_cache[uid2] = (datetime.now(), UserPermissions(user_id=uid2))

        invalidate_user_permissions_cache(None)

        assert len(_permission_cache) == 0

    def test_invalidate_nonexistent_user_is_noop(self):
        """Invalidating a user_id that is not cached does not raise."""
        invalidate_user_permissions_cache(str(uuid4()))
        assert len(_permission_cache) == 0

    def test_invalidate_accepts_uuid(self):
        """invalidate_user_permissions_cache accepts a UUID object."""
        uid = uuid4()
        _permission_cache[str(uid)] = (datetime.now(), UserPermissions(user_id=str(uid)))

        invalidate_user_permissions_cache(uid)

        assert str(uid) not in _permission_cache

    def test_invalidate_default_clears_all(self):
        """Calling with no arguments clears the entire cache."""
        uid = str(uuid4())
        _permission_cache[uid] = (datetime.now(), UserPermissions(user_id=uid))

        invalidate_user_permissions_cache()

        assert len(_permission_cache) == 0


# ---------------------------------------------------------------------------
# assign_role_to_user
# ---------------------------------------------------------------------------


class TestAssignRoleToUser:
    """Tests for assign_role_to_user()."""

    async def test_assign_role_success(self):
        """Successfully assigning a role returns True and commits."""
        user_id = uuid4()
        role = _make_role("editor")
        user = _make_user(user_id, roles=[])

        session = _make_session_with_execute(user, role)

        result = await assign_role_to_user(session, str(user_id), "editor")

        assert result is True
        assert role in user.roles
        session.commit.assert_awaited_once()

    async def test_assign_role_user_not_found(self):
        """Returns False when the user is not found."""
        role = _make_role("editor")
        session = _make_session_with_execute(None, role)

        result = await assign_role_to_user(session, str(uuid4()), "editor")

        assert result is False
        session.commit.assert_not_awaited()

    async def test_assign_role_role_not_found(self):
        """Returns False when the role is not found."""
        user_id = uuid4()
        user = _make_user(user_id)
        session = _make_session_with_execute(user, None)

        result = await assign_role_to_user(session, str(user_id), "nonexistent")

        assert result is False
        session.commit.assert_not_awaited()

    async def test_assign_role_already_assigned(self):
        """When the role is already assigned, returns True but does not commit again."""
        user_id = uuid4()
        role = _make_role("editor")
        user = _make_user(user_id, roles=[role])

        session = _make_session_with_execute(user, role)

        result = await assign_role_to_user(session, str(user_id), "editor")

        assert result is True
        # The role was already present, so no commit should happen
        session.commit.assert_not_awaited()

    async def test_assign_role_invalidates_cache(self):
        """Assigning a role invalidates that user's permission cache."""
        user_id = uuid4()
        _permission_cache[str(user_id)] = (
            datetime.now(),
            UserPermissions(user_id=str(user_id)),
        )

        role = _make_role("admin")
        user = _make_user(user_id, roles=[])
        session = _make_session_with_execute(user, role)

        await assign_role_to_user(session, str(user_id), "admin")

        assert str(user_id) not in _permission_cache

    async def test_assign_role_both_not_found(self):
        """Returns False when neither user nor role is found."""
        session = _make_session_with_execute(None, None)

        result = await assign_role_to_user(session, str(uuid4()), "ghost")

        assert result is False


# ---------------------------------------------------------------------------
# remove_role_from_user
# ---------------------------------------------------------------------------


class TestRemoveRoleFromUser:
    """Tests for remove_role_from_user()."""

    async def test_remove_role_success(self):
        """Removing an assigned role returns True and commits."""
        user_id = uuid4()
        role = _make_role("editor")
        user = _make_user(user_id, roles=[role])

        session = _make_session_with_execute(user)

        result = await remove_role_from_user(session, str(user_id), "editor")

        assert result is True
        assert role not in user.roles
        session.commit.assert_awaited_once()

    async def test_remove_role_user_not_found(self):
        """Returns False when the user does not exist."""
        session = _make_session_with_execute(None)

        result = await remove_role_from_user(session, str(uuid4()), "editor")

        assert result is False
        session.commit.assert_not_awaited()

    async def test_remove_role_not_in_user_roles(self):
        """Returns False when the user exists but does not have the role."""
        user_id = uuid4()
        user = _make_user(user_id, roles=[_make_role("author")])
        session = _make_session_with_execute(user)

        result = await remove_role_from_user(session, str(user_id), "editor")

        assert result is False
        session.commit.assert_not_awaited()

    async def test_remove_role_invalidates_cache(self):
        """Removing a role invalidates that user's permission cache."""
        user_id = uuid4()
        _permission_cache[str(user_id)] = (
            datetime.now(),
            UserPermissions(user_id=str(user_id)),
        )

        role = _make_role("editor")
        user = _make_user(user_id, roles=[role])
        session = _make_session_with_execute(user)

        await remove_role_from_user(session, str(user_id), "editor")

        assert str(user_id) not in _permission_cache

    async def test_remove_role_accepts_uuid(self):
        """remove_role_from_user accepts a UUID object for user_id."""
        user_id = uuid4()
        role = _make_role("admin")
        user = _make_user(user_id, roles=[role])
        session = _make_session_with_execute(user)

        result = await remove_role_from_user(session, user_id, "admin")

        assert result is True


# ---------------------------------------------------------------------------
# sync_roles_to_database
# ---------------------------------------------------------------------------


class TestSyncRolesToDatabase:
    """Tests for sync_roles_to_database()."""

    def _make_role_def(self, name, permissions, display_name=None, description=None):
        """Create a mock RoleDefinition."""
        rd = MagicMock()
        rd.name = name
        rd.permissions = set(permissions)
        rd.display_name = display_name or name.title()
        rd.description = description
        return rd

    async def test_creates_new_roles(self):
        """New role definitions are inserted into the database."""
        role_def = self._make_role_def("reviewer", ["review-pages"], "Reviewer", "Can review")

        session = AsyncMock()
        session.add = MagicMock()
        # select(Role).where(Role.name == ...) returns None => new role
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        with patch("skrift.auth.roles.ROLE_DEFINITIONS", {"reviewer": role_def}):
            await sync_roles_to_database(session)

        # session.add should be called for the new Role and its RolePermission
        assert session.add.call_count >= 2
        session.flush.assert_awaited()
        session.commit.assert_awaited_once()

    async def test_updates_existing_roles(self):
        """Existing roles get their display_name and description updated."""
        role_def = self._make_role_def("editor", ["manage-pages"], "Editor v2", "Updated desc")

        existing_role = MagicMock()
        existing_role.id = uuid4()
        existing_role.name = "editor"
        existing_role.display_name = "Editor"
        existing_role.description = "Old desc"

        session = AsyncMock()
        session.add = MagicMock()
        mock_select_result = MagicMock()
        mock_select_result.scalar_one_or_none.return_value = existing_role
        # First execute: select Role, subsequent executes: delete permissions
        session.execute = AsyncMock(return_value=mock_select_result)

        with patch("skrift.auth.roles.ROLE_DEFINITIONS", {"editor": role_def}):
            await sync_roles_to_database(session)

        assert existing_role.display_name == "Editor v2"
        assert existing_role.description == "Updated desc"
        session.commit.assert_awaited_once()

    async def test_replaces_permissions_for_existing_role(self):
        """Old permissions are deleted and new ones are added for existing roles."""
        role_def = self._make_role_def("editor", ["manage-pages", "view-drafts"])

        existing_role = MagicMock()
        existing_role.id = uuid4()

        session = AsyncMock()
        session.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_role
        session.execute = AsyncMock(return_value=mock_result)

        with patch("skrift.auth.roles.ROLE_DEFINITIONS", {"editor": role_def}):
            await sync_roles_to_database(session)

        # execute is called: once for the select, once for the delete
        assert session.execute.await_count >= 2
        # session.add should be called for each new permission
        assert session.add.call_count == 2
        session.commit.assert_awaited_once()

    async def test_sync_multiple_roles(self):
        """Multiple role definitions are all synced."""
        role_a = self._make_role_def("admin", ["administrator"])
        role_b = self._make_role_def("author", ["view-drafts"])

        session = AsyncMock()
        session.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        with patch("skrift.auth.roles.ROLE_DEFINITIONS", {"admin": role_a, "author": role_b}):
            await sync_roles_to_database(session)

        # flush should be called once per new role
        assert session.flush.await_count == 2
        session.commit.assert_awaited_once()

    async def test_sync_invalidates_all_caches(self):
        """sync_roles_to_database clears the entire permission cache."""
        uid1 = str(uuid4())
        uid2 = str(uuid4())
        _permission_cache[uid1] = (datetime.now(), UserPermissions(user_id=uid1))
        _permission_cache[uid2] = (datetime.now(), UserPermissions(user_id=uid2))

        session = AsyncMock()
        # No role definitions to process
        with patch("skrift.auth.roles.ROLE_DEFINITIONS", {}):
            await sync_roles_to_database(session)

        assert len(_permission_cache) == 0

    async def test_creates_role_permission_objects(self):
        """Each permission string produces a RolePermission added to the session."""
        role_def = self._make_role_def("admin", ["administrator", "manage-users", "manage-pages"])

        session = AsyncMock()
        session.add = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        with patch("skrift.auth.roles.ROLE_DEFINITIONS", {"admin": role_def}):
            await sync_roles_to_database(session)

        # session.add is called once for the new Role + once per permission (3)
        assert session.add.call_count == 4
        # The first add call is the Role itself
        first_added = session.add.call_args_list[0][0][0]
        assert isinstance(first_added, Role)
        # The remaining add calls are RolePermission objects
        added_perms = [call[0][0] for call in session.add.call_args_list[1:]]
        for rp in added_perms:
            assert isinstance(rp, RolePermission)
        added_perm_strings = {rp.permission for rp in added_perms}
        assert added_perm_strings == {"administrator", "manage-users", "manage-pages"}
