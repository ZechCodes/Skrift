"""Role definitions for the application."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoleDefinition:
    """Definition of a role with its permissions."""

    name: str
    permissions: set[str] = field(default_factory=set)
    display_name: str | None = None
    description: str | None = None


def create_role(
    name: str,
    *permissions: str,
    display_name: str | None = None,
    description: str | None = None,
) -> RoleDefinition:
    """Create a role definition with the given permissions.

    Args:
        name: The unique identifier for the role
        *permissions: Permission strings granted by this role
        display_name: Human-readable name for the role
        description: Description of the role's purpose

    Returns:
        A RoleDefinition instance
    """
    return RoleDefinition(
        name=name,
        permissions=set(permissions),
        display_name=display_name or name.title(),
        description=description,
    )


# Default role definitions
# The "administrator" permission is special - it bypasses all permission checks

ADMIN = create_role(
    "admin",
    "administrator",
    "manage-users",
    "manage-pages",
    "modify-site",
    display_name="Administrator",
    description="Full system access with all permissions",
)

AUTHOR = create_role(
    "author",
    "view-drafts",
    "edit-own-pages",
    "delete-own-pages",
    "create-pages",
    "upload-media",
    display_name="Author",
    description="Can create and manage own pages",
)

EDITOR = create_role(
    "editor",
    "view-drafts",
    "manage-pages",
    "create-pages",
    "manage-media",
    display_name="Editor",
    description="Can manage all pages and view drafts",
)

MODERATOR = create_role(
    "moderator",
    "view-drafts",
    "manage-pages",
    "create-pages",
    "manage-media",
    display_name="Moderator",
    description="Can moderate content and manage pages",
)

# Registry of all role definitions
ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    role.name: role for role in [ADMIN, AUTHOR, EDITOR, MODERATOR]
}


def permissions_for_type(plural: str) -> dict[str, str]:
    """Generate permission strings for a page type.

    Returns a dict with keys: manage, create, edit_own, delete_own.
    """
    return {
        "manage": f"manage-{plural}",
        "create": f"create-{plural}",
        "edit_own": f"edit-own-{plural}",
        "delete_own": f"delete-own-{plural}",
    }


def expand_roles_for_page_types(page_types: list) -> None:
    """Add type-specific permissions to default roles. Called at startup."""
    for pt in page_types:
        perms = permissions_for_type(pt.plural)
        ADMIN.permissions.add(perms["manage"])
        AUTHOR.permissions.update({perms["edit_own"], perms["delete_own"], perms["create"]})
        EDITOR.permissions.update({perms["manage"], perms["create"]})
        MODERATOR.permissions.update({perms["manage"], perms["create"]})


def get_role_definition(name: str) -> RoleDefinition | None:
    """Get a role definition by name."""
    return ROLE_DEFINITIONS.get(name)


def register_role(
    name: str,
    *permissions: str,
    display_name: str | None = None,
    description: str | None = None,
) -> RoleDefinition:
    """Register a custom role definition.

    This allows applications to add custom roles beyond the defaults.
    Call this during application startup (e.g., in a custom controller module
    or app initialization) before the database sync occurs.

    Args:
        name: The unique identifier for the role
        *permissions: Permission strings granted by this role
        display_name: Human-readable name for the role
        description: Description of the role's purpose

    Returns:
        The registered RoleDefinition instance

    Example:
        from skrift.auth.roles import register_role

        # Register a custom role with permissions
        register_role(
            "support",
            "view-tickets",
            "respond-tickets",
            display_name="Support Agent",
            description="Can view and respond to support tickets",
        )
    """
    role = create_role(
        name,
        *permissions,
        display_name=display_name,
        description=description,
    )
    ROLE_DEFINITIONS[role.name] = role
    return role
