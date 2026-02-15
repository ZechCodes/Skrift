"""Permission and Role guards for Litestar routes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers import BaseRouteHandler

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


async def auth_guard(
    connection: ASGIConnection, route_handler: BaseRouteHandler
) -> None:
    """Litestar guard that checks authentication and authorization requirements.

    This guard checks the guards parameter of route handlers for AuthRequirement
    instances and validates them against the user's permissions and roles.
    """
    from skrift.auth.services import get_user_permissions

    # Get user_id from session
    user_id = connection.session.get("user_id") if connection.session else None

    if not user_id:
        raise NotAuthorizedException("Authentication required")

    # Get the guards from the route handler
    guards = route_handler.guards or []

    # Find AuthRequirement guards
    auth_requirements = [g for g in guards if isinstance(g, AuthRequirement)]

    if not auth_requirements:
        return  # No auth requirements, just needs to be logged in

    # Get user's permissions and roles
    session_maker = connection.app.state.session_maker_class
    async with session_maker() as session:
        permissions = await get_user_permissions(session, user_id)

    # Check all requirements
    for requirement in auth_requirements:
        if not await requirement.check(permissions):
            raise NotAuthorizedException("Insufficient permissions")
