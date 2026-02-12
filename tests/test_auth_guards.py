"""Tests for the auth guards module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skrift.auth.guards import (
    ADMINISTRATOR_PERMISSION,
    AndRequirement,
    AuthRequirement,
    OrRequirement,
    Permission,
    Role,
    auth_guard,
)
from skrift.auth.services import UserPermissions

from litestar.exceptions import NotAuthorizedException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_permissions(
    user_id: str = "user-1",
    roles: set[str] | None = None,
    permissions: set[str] | None = None,
) -> UserPermissions:
    """Create a UserPermissions instance for testing."""
    return UserPermissions(
        user_id=user_id,
        roles=roles or set(),
        permissions=permissions or set(),
    )


# ===========================================================================
# Permission.check()
# ===========================================================================

class TestPermissionCheck:
    """Tests for Permission.check()."""

    @pytest.mark.asyncio
    async def test_has_permission(self):
        """User with the required permission passes the check."""
        perm = Permission("edit_page")
        user_perms = _make_permissions(permissions={"edit_page", "view_page"})

        assert await perm.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_does_not_have_permission(self):
        """User without the required permission fails the check."""
        perm = Permission("delete_page")
        user_perms = _make_permissions(permissions={"edit_page", "view_page"})

        assert await perm.check(user_perms) is False

    @pytest.mark.asyncio
    async def test_administrator_bypass(self):
        """User with administrator permission passes any permission check."""
        perm = Permission("some_obscure_permission")
        user_perms = _make_permissions(permissions={ADMINISTRATOR_PERMISSION})

        assert await perm.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_empty_permissions(self):
        """User with no permissions at all fails the check."""
        perm = Permission("edit_page")
        user_perms = _make_permissions(permissions=set())

        assert await perm.check(user_perms) is False


# ===========================================================================
# Role.check()
# ===========================================================================

class TestRoleCheck:
    """Tests for Role.check()."""

    @pytest.mark.asyncio
    async def test_has_role(self):
        """User with the required role passes the check."""
        role = Role("editor")
        user_perms = _make_permissions(roles={"editor", "viewer"})

        assert await role.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_does_not_have_role(self):
        """User without the required role fails the check."""
        role = Role("admin")
        user_perms = _make_permissions(roles={"editor", "viewer"})

        assert await role.check(user_perms) is False

    @pytest.mark.asyncio
    async def test_administrator_bypass(self):
        """User with administrator permission passes any role check."""
        role = Role("super_special_role")
        user_perms = _make_permissions(
            roles=set(),
            permissions={ADMINISTRATOR_PERMISSION},
        )

        assert await role.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_empty_roles(self):
        """User with no roles at all fails the check."""
        role = Role("editor")
        user_perms = _make_permissions(roles=set())

        assert await role.check(user_perms) is False


# ===========================================================================
# OrRequirement
# ===========================================================================

class TestOrRequirement:
    """Tests for OrRequirement.check()."""

    @pytest.mark.asyncio
    async def test_left_true(self):
        """Passes when left requirement is satisfied (right is not)."""
        req = OrRequirement(Permission("edit_page"), Permission("delete_page"))
        user_perms = _make_permissions(permissions={"edit_page"})

        assert await req.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_right_true(self):
        """Passes when right requirement is satisfied (left is not)."""
        req = OrRequirement(Permission("delete_page"), Permission("edit_page"))
        user_perms = _make_permissions(permissions={"edit_page"})

        assert await req.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_both_true(self):
        """Passes when both requirements are satisfied."""
        req = OrRequirement(Permission("edit_page"), Permission("view_page"))
        user_perms = _make_permissions(permissions={"edit_page", "view_page"})

        assert await req.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_both_false(self):
        """Fails when neither requirement is satisfied."""
        req = OrRequirement(Permission("edit_page"), Permission("delete_page"))
        user_perms = _make_permissions(permissions={"view_page"})

        assert await req.check(user_perms) is False


# ===========================================================================
# AndRequirement
# ===========================================================================

class TestAndRequirement:
    """Tests for AndRequirement.check()."""

    @pytest.mark.asyncio
    async def test_both_true(self):
        """Passes when both requirements are satisfied."""
        req = AndRequirement(Permission("edit_page"), Permission("view_page"))
        user_perms = _make_permissions(permissions={"edit_page", "view_page"})

        assert await req.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_left_false(self):
        """Fails when the left requirement is not satisfied."""
        req = AndRequirement(Permission("edit_page"), Permission("view_page"))
        user_perms = _make_permissions(permissions={"view_page"})

        assert await req.check(user_perms) is False

    @pytest.mark.asyncio
    async def test_right_false(self):
        """Fails when the right requirement is not satisfied."""
        req = AndRequirement(Permission("edit_page"), Permission("view_page"))
        user_perms = _make_permissions(permissions={"edit_page"})

        assert await req.check(user_perms) is False

    @pytest.mark.asyncio
    async def test_both_false(self):
        """Fails when neither requirement is satisfied."""
        req = AndRequirement(Permission("edit_page"), Permission("view_page"))
        user_perms = _make_permissions(permissions={"delete_page"})

        assert await req.check(user_perms) is False


# ===========================================================================
# Operator overloading
# ===========================================================================

class TestOperatorOverloading:
    """Tests for __or__ and __and__ on AuthRequirement subclasses."""

    def test_or_operator_returns_or_requirement(self):
        """Permission('a') | Permission('b') produces an OrRequirement."""
        result = Permission("a") | Permission("b")

        assert isinstance(result, OrRequirement)
        assert isinstance(result.left, Permission)
        assert isinstance(result.right, Permission)
        assert result.left.permission == "a"
        assert result.right.permission == "b"

    def test_and_operator_returns_and_requirement(self):
        """Permission('a') & Role('admin') produces an AndRequirement."""
        result = Permission("a") & Role("admin")

        assert isinstance(result, AndRequirement)
        assert isinstance(result.left, Permission)
        assert isinstance(result.right, Role)
        assert result.left.permission == "a"
        assert result.right.role == "admin"

    @pytest.mark.asyncio
    async def test_or_operator_check(self):
        """Composed OR requirement works correctly for permission checking."""
        req = Permission("a") | Permission("b")
        user_perms = _make_permissions(permissions={"b"})

        assert await req.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_and_operator_check(self):
        """Composed AND requirement works correctly for permission checking."""
        req = Permission("edit") & Role("editor")
        user_perms = _make_permissions(
            permissions={"edit"},
            roles={"editor"},
        )

        assert await req.check(user_perms) is True

    @pytest.mark.asyncio
    async def test_and_operator_check_fails_partial(self):
        """Composed AND requirement fails when only one side is satisfied."""
        req = Permission("edit") & Role("editor")
        user_perms = _make_permissions(
            permissions={"edit"},
            roles={"viewer"},
        )

        assert await req.check(user_perms) is False

    def test_chained_operators(self):
        """Chaining operators produces nested composite requirements."""
        result = Permission("a") | Permission("b") | Permission("c")

        # (a | b) | c  -- left-to-right associativity
        assert isinstance(result, OrRequirement)
        assert isinstance(result.left, OrRequirement)
        assert isinstance(result.right, Permission)


# ===========================================================================
# auth_guard()
# ===========================================================================

class TestAuthGuard:
    """Tests for the auth_guard() async function."""

    def _make_connection(self, session_data: dict | None = None):
        """Create a mock ASGIConnection with optional session data."""
        connection = MagicMock()
        connection.session = session_data
        connection.app.state.session_maker_class = MagicMock()
        return connection

    def _make_route_handler(self, guards: list | None = None):
        """Create a mock BaseRouteHandler with optional guards."""
        handler = MagicMock()
        handler.guards = guards
        return handler

    @pytest.mark.asyncio
    async def test_no_session_raises(self):
        """Raises NotAuthorizedException when connection.session is None."""
        connection = self._make_connection(session_data=None)
        handler = self._make_route_handler()

        with pytest.raises(NotAuthorizedException, match="Authentication required"):
            await auth_guard(connection, handler)

    @pytest.mark.asyncio
    async def test_no_user_id_raises(self):
        """Raises NotAuthorizedException when session has no user_id."""
        connection = self._make_connection(session_data={"some_key": "value"})
        handler = self._make_route_handler()

        with pytest.raises(NotAuthorizedException, match="Authentication required"):
            await auth_guard(connection, handler)

    @pytest.mark.asyncio
    async def test_empty_session_raises(self):
        """Raises NotAuthorizedException when session is an empty dict."""
        connection = self._make_connection(session_data={})
        handler = self._make_route_handler()

        with pytest.raises(NotAuthorizedException, match="Authentication required"):
            await auth_guard(connection, handler)

    @pytest.mark.asyncio
    async def test_no_auth_requirements_just_login(self):
        """Passes when user is logged in and there are no AuthRequirement guards."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        # Guards list contains no AuthRequirement instances (e.g. a plain callable).
        non_auth_guard = MagicMock()
        handler = self._make_route_handler(guards=[non_auth_guard])

        # Should return without raising.
        result = await auth_guard(connection, handler)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_guards_at_all_just_login(self):
        """Passes when user is logged in and handler.guards is None."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        handler = self._make_route_handler(guards=None)

        result = await auth_guard(connection, handler)
        assert result is None

    @pytest.mark.asyncio
    async def test_auth_requirements_met(self):
        """Passes when user has the required permissions."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        perm_guard = Permission("edit_page")
        handler = self._make_route_handler(guards=[perm_guard])

        user_perms = _make_permissions(
            user_id="user-1",
            permissions={"edit_page"},
        )

        # Mock the session maker context manager and get_user_permissions
        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)
        connection.app.state.session_maker_class = mock_session_maker

        with patch(
            "skrift.auth.services.get_user_permissions",
            new_callable=AsyncMock,
            return_value=user_perms,
        ):
            result = await auth_guard(connection, handler)
            assert result is None

    @pytest.mark.asyncio
    async def test_auth_requirements_not_met(self):
        """Raises NotAuthorizedException when user lacks required permissions."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        perm_guard = Permission("delete_page")
        handler = self._make_route_handler(guards=[perm_guard])

        user_perms = _make_permissions(
            user_id="user-1",
            permissions={"view_page"},
        )

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)
        connection.app.state.session_maker_class = mock_session_maker

        with patch(
            "skrift.auth.services.get_user_permissions",
            new_callable=AsyncMock,
            return_value=user_perms,
        ):
            with pytest.raises(NotAuthorizedException, match="Insufficient permissions"):
                await auth_guard(connection, handler)

    @pytest.mark.asyncio
    async def test_multiple_auth_requirements_all_met(self):
        """Passes when all AuthRequirement guards in the list are satisfied."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        guard_a = Permission("edit_page")
        guard_b = Role("editor")
        handler = self._make_route_handler(guards=[guard_a, guard_b])

        user_perms = _make_permissions(
            user_id="user-1",
            permissions={"edit_page"},
            roles={"editor"},
        )

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)
        connection.app.state.session_maker_class = mock_session_maker

        with patch(
            "skrift.auth.services.get_user_permissions",
            new_callable=AsyncMock,
            return_value=user_perms,
        ):
            result = await auth_guard(connection, handler)
            assert result is None

    @pytest.mark.asyncio
    async def test_multiple_auth_requirements_one_fails(self):
        """Raises when one of multiple AuthRequirement guards fails."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        guard_a = Permission("edit_page")
        guard_b = Permission("delete_page")
        handler = self._make_route_handler(guards=[guard_a, guard_b])

        user_perms = _make_permissions(
            user_id="user-1",
            permissions={"edit_page"},  # has edit_page but not delete_page
        )

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)
        connection.app.state.session_maker_class = mock_session_maker

        with patch(
            "skrift.auth.services.get_user_permissions",
            new_callable=AsyncMock,
            return_value=user_perms,
        ):
            with pytest.raises(NotAuthorizedException, match="Insufficient permissions"):
                await auth_guard(connection, handler)

    @pytest.mark.asyncio
    async def test_mixed_guards_ignores_non_auth(self):
        """Non-AuthRequirement callables in guards list are ignored by auth_guard."""
        connection = self._make_connection(session_data={"user_id": "user-1"})
        plain_guard = MagicMock()  # Not an AuthRequirement
        perm_guard = Permission("view_page")
        handler = self._make_route_handler(guards=[plain_guard, perm_guard])

        user_perms = _make_permissions(
            user_id="user-1",
            permissions={"view_page"},
        )

        mock_session = AsyncMock()
        mock_session_maker = MagicMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)
        connection.app.state.session_maker_class = mock_session_maker

        with patch(
            "skrift.auth.services.get_user_permissions",
            new_callable=AsyncMock,
            return_value=user_perms,
        ):
            result = await auth_guard(connection, handler)
            assert result is None
