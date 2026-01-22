"""Navigation service for building admin nav from route introspection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from litestar.routes import HTTPRoute

from skrift.auth.guards import AuthRequirement, OrRequirement, AndRequirement

if TYPE_CHECKING:
    from litestar import Litestar
    from skrift.auth.services import UserPermissions


ADMIN_NAV_TAG = "admin-nav"


@dataclass
class AdminNavItem:
    """Represents a navigation item in the admin sidebar."""

    path: str
    label: str
    icon: str = "circle"
    order: int = 100


async def check_requirement(
    requirement: AuthRequirement, permissions: "UserPermissions"
) -> bool:
    """Recursively check if a requirement is satisfied."""
    if isinstance(requirement, OrRequirement):
        left_ok = await check_requirement(requirement.left, permissions)
        right_ok = await check_requirement(requirement.right, permissions)
        return left_ok or right_ok
    elif isinstance(requirement, AndRequirement):
        left_ok = await check_requirement(requirement.left, permissions)
        right_ok = await check_requirement(requirement.right, permissions)
        return left_ok and right_ok
    else:
        return await requirement.check(permissions)


async def build_admin_nav(
    app: "Litestar",
    user_permissions: "UserPermissions",
    current_path: str | None = None,
) -> list[AdminNavItem]:
    """Build admin navigation by introspecting routes.

    Iterates through app routes to find handlers tagged with ADMIN_NAV_TAG,
    extracts their permission guards, checks them against user permissions,
    and returns accessible nav items sorted by order.

    Args:
        app: The Litestar application instance
        user_permissions: The current user's permissions
        current_path: The current request path (for highlighting active nav)

    Returns:
        List of AdminNavItem for routes the user can access
    """
    nav_items: list[AdminNavItem] = []

    for route in app.routes:
        if not isinstance(route, HTTPRoute):
            continue

        for handler in route.route_handlers:
            # Check if handler has the admin-nav tag
            if not hasattr(handler, "tags") or ADMIN_NAV_TAG not in (handler.tags or []):
                continue

            # Check if handler has opt metadata with label
            opt = getattr(handler, "opt", {}) or {}
            if "label" not in opt:
                continue

            # Extract auth requirement guards
            guards = handler.guards or []
            auth_requirements = [g for g in guards if isinstance(g, AuthRequirement)]

            # Check all requirements
            can_access = True
            for requirement in auth_requirements:
                if not await check_requirement(requirement, user_permissions):
                    can_access = False
                    break

            if can_access:
                nav_items.append(
                    AdminNavItem(
                        path=route.path,
                        label=opt["label"],
                        icon=opt.get("icon", "circle"),
                        order=opt.get("order", 100),
                    )
                )

    # Sort by order, then by label
    nav_items.sort(key=lambda x: (x.order, x.label))

    return nav_items
