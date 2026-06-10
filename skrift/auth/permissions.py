"""Permission metadata and API grant clearance declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PermissionClearance = Literal[
    "disallow-api-grants",
    "allow-anonymous-service",
    "require-known-service",
    "require-elevated-security",
]

DISALLOW_API_GRANTS: PermissionClearance = "disallow-api-grants"
ALLOW_ANONYMOUS_SERVICE: PermissionClearance = "allow-anonymous-service"
REQUIRE_KNOWN_SERVICE: PermissionClearance = "require-known-service"
REQUIRE_ELEVATED_SECURITY: PermissionClearance = "require-elevated-security"

_CLEARANCE_RANK: dict[PermissionClearance, int] = {
    DISALLOW_API_GRANTS: 0,
    ALLOW_ANONYMOUS_SERVICE: 1,
    REQUIRE_KNOWN_SERVICE: 2,
    REQUIRE_ELEVATED_SECURITY: 3,
}


@dataclass(frozen=True)
class PermissionDefinition:
    """Human-facing metadata for a permission slug."""

    slug: str
    display_name: str
    description: str = ""
    service_clearance: PermissionClearance = DISALLOW_API_GRANTS


PERMISSION_DEFINITIONS: dict[str, PermissionDefinition] = {}


def _humanize_permission(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def register_permission(
    slug: str,
    *,
    display_name: str | None = None,
    description: str = "",
    service_clearance: PermissionClearance = DISALLOW_API_GRANTS,
) -> PermissionDefinition:
    """Register or replace a permission definition.

    Permissions default to ``disallow-api-grants`` so a new route permission
    never becomes externally grantable unless the app opts it in.
    """
    definition = PermissionDefinition(
        slug=slug,
        display_name=display_name or _humanize_permission(slug),
        description=description,
        service_clearance=service_clearance,
    )
    PERMISSION_DEFINITIONS[slug] = definition
    return definition


def ensure_permission(
    slug: str,
    *,
    display_name: str | None = None,
    description: str = "",
    service_clearance: PermissionClearance = DISALLOW_API_GRANTS,
) -> PermissionDefinition:
    """Register a permission only if no explicit definition exists."""
    existing = PERMISSION_DEFINITIONS.get(slug)
    if existing is not None:
        return existing
    return register_permission(
        slug,
        display_name=display_name,
        description=description,
        service_clearance=service_clearance,
    )


def get_permission_definition(slug: str) -> PermissionDefinition:
    """Return a permission definition, creating a non-grantable fallback."""
    return ensure_permission(slug)


def list_permission_definitions() -> list[PermissionDefinition]:
    """Return all known permission definitions sorted by display name."""
    return sorted(PERMISSION_DEFINITIONS.values(), key=lambda p: (p.display_name, p.slug))


def strictest_clearance(
    permissions: list[str] | set[str] | tuple[str, ...],
) -> PermissionClearance:
    """Return the strictest service clearance required by the permission set."""
    strictest: PermissionClearance = ALLOW_ANONYMOUS_SERVICE
    strictest_rank = _CLEARANCE_RANK[strictest]
    for permission in permissions:
        definition = get_permission_definition(permission)
        rank = _CLEARANCE_RANK[definition.service_clearance]
        if rank == 0:
            return DISALLOW_API_GRANTS
        if rank > strictest_rank:
            strictest = definition.service_clearance
            strictest_rank = rank
    return strictest


def register_builtin_permissions() -> None:
    """Register Skrift's built-in permissions with conservative grant defaults."""
    builtin_descriptions = {
        "administrator": "Bypass all permission checks and administer the entire site.",
        "manage-users": "Create, edit, activate, deactivate, and assign roles to users.",
        "manage-pages": "Create, edit, publish, schedule, and delete all pages.",
        "modify-site": "Change site-wide settings and theme configuration.",
        "manage-oauth-clients": "Manage OAuth clients that can authenticate against this site.",
        "manage-api-keys": "Create, rotate, revoke, and delete API keys.",
        "view-drafts": "View unpublished content.",
        "edit-own-pages": "Edit pages owned by the current user.",
        "delete-own-pages": "Delete pages owned by the current user.",
        "create-pages": "Create new pages.",
        "upload-media": "Upload media assets.",
        "manage-media": "Manage all media assets.",
    }
    for slug, description in builtin_descriptions.items():
        ensure_permission(slug, description=description)


register_builtin_permissions()
