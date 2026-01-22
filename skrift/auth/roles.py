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
    display_name="Author",
    description="Can view draft content",
)

EDITOR = create_role(
    "editor",
    "view-drafts",
    "manage-pages",
    display_name="Editor",
    description="Can manage pages and view drafts",
)

MODERATOR = create_role(
    "moderator",
    "view-drafts",
    display_name="Moderator",
    description="Can moderate content",
)

# Registry of all role definitions
ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    role.name: role for role in [ADMIN, AUTHOR, EDITOR, MODERATOR]
}


def get_role_definition(name: str) -> RoleDefinition | None:
    """Get a role definition by name."""
    return ROLE_DEFINITIONS.get(name)


def register_role(role: RoleDefinition) -> None:
    """Register a custom role definition.

    This allows applications to add custom roles beyond the defaults.
    """
    ROLE_DEFINITIONS[role.name] = role
