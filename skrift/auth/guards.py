"""Permission and Role guards for Litestar routes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers import BaseRouteHandler

from skrift.auth.session_keys import SESSION_USER_ID

if TYPE_CHECKING:
    from skrift.auth.services import UserPermissions


ADMINISTRATOR_PERMISSION = "administrator"


class AuthRequirement(ABC):
    """Base class for authorization requirements with operator overloading."""

    @abstractmethod
    async def check(self, permissions: "UserPermissions") -> bool:
        """Check if the requirement is satisfied."""
        ...

    def __or__(self, other: "AuthRequirement") -> "OrRequirement":
        """Combine requirements with OR logic."""
        return OrRequirement(self, other)

    def __and__(self, other: "AuthRequirement") -> "AndRequirement":
        """Combine requirements with AND logic."""
        return AndRequirement(self, other)

    def __call__(
        self, connection: ASGIConnection, _: BaseRouteHandler
    ) -> None:
        """Guard function for use with Litestar guards parameter.

        This is a synchronous wrapper - actual checking happens in the async guard.
        """
        pass


class OrRequirement(AuthRequirement):
    """Combines two requirements with OR logic."""

    def __init__(self, left: AuthRequirement, right: AuthRequirement):
        self.left = left
        self.right = right

    async def check(self, permissions: "UserPermissions") -> bool:
        """Return True if either requirement is satisfied."""
        return await self.left.check(permissions) or await self.right.check(permissions)


class AndRequirement(AuthRequirement):
    """Combines two requirements with AND logic."""

    def __init__(self, left: AuthRequirement, right: AuthRequirement):
        self.left = left
        self.right = right

    async def check(self, permissions: "UserPermissions") -> bool:
        """Return True if both requirements are satisfied."""
        return await self.left.check(permissions) and await self.right.check(permissions)


class Permission(AuthRequirement):
    """Permission requirement for route guards."""

    def __init__(self, permission: str):
        self.permission = permission

    async def check(self, permissions: "UserPermissions") -> bool:
        """Check if user has the required permission or administrator permission."""
        # Administrator permission bypasses all checks
        if ADMINISTRATOR_PERMISSION in permissions.permissions:
            return True
        return self.permission in permissions.permissions


class OwnerOrPermission(AuthRequirement):
    """Gate that passes if user has either the 'own' permission or the 'any' permission.

    Actual ownership verification happens in the handler — this guard only checks
    that the user has at least one of the required permission levels.
    """

    def __init__(self, own_permission: str, any_permission: str):
        self.own_permission = own_permission
        self.any_permission = any_permission

    async def check(self, permissions: "UserPermissions") -> bool:
        if ADMINISTRATOR_PERMISSION in permissions.permissions:
            return True
        return self.any_permission in permissions.permissions or self.own_permission in permissions.permissions


class Role(AuthRequirement):
    """Role requirement for route guards."""

    def __init__(self, role: str):
        self.role = role

    async def check(self, permissions: "UserPermissions") -> bool:
        """Check if user has the required role or administrator permission."""
        # Administrator permission bypasses all checks
        if ADMINISTRATOR_PERMISSION in permissions.permissions:
            return True
        return self.role in permissions.roles


class APIKeyAuth(AuthRequirement):
    """Marker guard: route accepts both session and API key authentication.

    When present in a route's guards list, ``auth_guard`` will accept
    ``Authorization: Bearer sk_...`` headers in addition to session cookies.

    Usage::

        @get("/api/pages", guards=[auth_guard, APIKeyAuth(), Permission("manage-pages")])
        async def list_pages(self): ...
    """

    async def check(self, permissions: "UserPermissions") -> bool:
        return True  # Marker only — actual verification happens in auth_guard


class APIKeyOnly(AuthRequirement):
    """Marker guard: route accepts ONLY API key authentication (no session).

    Usage::

        @post("/api/v1/sync", guards=[auth_guard, APIKeyOnly(), Permission("manage-pages")])
        async def sync(self): ...
    """

    async def check(self, permissions: "UserPermissions") -> bool:
        return True  # Marker only — actual verification happens in auth_guard


def _extract_bearer_token(connection: ASGIConnection) -> str | None:
    """Extract a Bearer token from the Authorization header."""
    auth_header = connection.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


def _get_client_ip(connection: ASGIConnection) -> str | None:
    """Best-effort client IP from the connection."""
    forwarded = connection.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = connection.scope.get("client")
    if client:
        return client[0]
    return None


async def _resolve_api_key_permissions(
    connection: ASGIConnection,
    bearer: str,
) -> tuple[str | None, "UserPermissions | None"]:
    """Verify an API key bearer token and return (user_id, scoped_permissions).

    Returns (None, None) if the token is invalid.
    """
    from skrift.auth.services import UserPermissions, get_user_permissions
    from skrift.db.services import api_key_service

    session_maker = connection.app.state.session_maker_class
    async with session_maker() as db_session:
        api_key = await api_key_service.verify_api_key(
            db_session, bearer, client_ip=_get_client_ip(connection)
        )
        if api_key is None:
            return None, None

        user_id = str(api_key.user_id)

        # Get the user's full permissions
        user_perms = await get_user_permissions(db_session, user_id)

        # Apply key scoping: intersection of user permissions and key scope
        key_permissions = set(api_key.scoped_permission_list)
        key_roles = set(api_key.scoped_role_list)

        if not key_permissions and not key_roles:
            # No scoping — inherit all user permissions
            return user_id, user_perms

        # Build scoped permission set
        scoped = set()

        # Add permissions from scoped roles
        if key_roles:
            from skrift.auth.roles import ROLE_DEFINITIONS

            for role_name in key_roles & user_perms.roles:
                role_def = ROLE_DEFINITIONS.get(role_name)
                if role_def:
                    scoped.update(role_def.permissions)

        # Add directly scoped permissions
        if key_permissions:
            scoped.update(key_permissions)

        # Intersect with user's actual permissions
        effective_permissions = scoped & user_perms.permissions
        effective_roles = key_roles & user_perms.roles if key_roles else set()

        return user_id, UserPermissions(
            user_id=user_id,
            roles=effective_roles,
            permissions=effective_permissions,
        )


async def auth_guard(
    connection: ASGIConnection, route_handler: BaseRouteHandler
) -> None:
    """Litestar guard that checks authentication and authorization requirements.

    Supports session-based auth (default), API key auth (when ``APIKeyAuth``
    or ``APIKeyOnly`` markers are present in the route guards), or both.
    """
    from skrift.auth.services import get_user_permissions

    # Get the guards from the route handler
    guards = route_handler.guards or []

    # Determine auth mode from guard markers
    api_only = any(isinstance(g, APIKeyOnly) for g in guards)
    api_compatible = api_only or any(isinstance(g, APIKeyAuth) for g in guards)

    user_id: str | None = None
    permissions: UserPermissions | None = None

    # Try API key auth if route supports it
    if api_compatible:
        bearer = _extract_bearer_token(connection)
        if bearer and bearer.startswith("sk_"):
            user_id, permissions = await _resolve_api_key_permissions(connection, bearer)

    # Fall back to session auth (unless API-only)
    if not user_id and not api_only:
        user_id = connection.session.get(SESSION_USER_ID) if connection.session else None
        if user_id:
            session_maker = connection.app.state.session_maker_class
            async with session_maker() as session:
                permissions = await get_user_permissions(session, user_id)

    if not user_id or permissions is None:
        raise NotAuthorizedException("Authentication required")

    # Find AuthRequirement guards (exclude marker guards)
    auth_requirements = [
        g for g in guards
        if isinstance(g, AuthRequirement)
        and not isinstance(g, (APIKeyAuth, APIKeyOnly))
    ]

    if not auth_requirements:
        return  # No auth requirements, just needs to be logged in

    # Check all requirements
    for requirement in auth_requirements:
        if not await requirement.check(permissions):
            raise NotAuthorizedException("Insufficient permissions")
