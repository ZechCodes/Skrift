"""Authentication and authorization module."""

from skrift.auth.guards import (
    ADMINISTRATOR_PERMISSION,
    AndRequirement,
    AuthRequirement,
    OrRequirement,
    Permission,
    Role,
    auth_guard,
)
from skrift.auth.roles import (
    ADMIN,
    AUTHOR,
    EDITOR,
    MODERATOR,
    ROLE_DEFINITIONS,
    RoleDefinition,
    create_role,
    get_role_definition,
    register_role,
)
from skrift.auth.services import (
    UserPermissions,
    assign_role_to_user,
    get_user_permissions,
    invalidate_user_permissions_cache,
    remove_role_from_user,
    sync_roles_to_database,
)

__all__ = [
    # Guards
    "ADMINISTRATOR_PERMISSION",
    "AndRequirement",
    "AuthRequirement",
    "OrRequirement",
    "Permission",
    "Role",
    "auth_guard",
    # Roles
    "ADMIN",
    "AUTHOR",
    "EDITOR",
    "MODERATOR",
    "ROLE_DEFINITIONS",
    "RoleDefinition",
    "create_role",
    "get_role_definition",
    "register_role",
    # Services
    "UserPermissions",
    "assign_role_to_user",
    "get_user_permissions",
    "invalidate_user_permissions_cache",
    "remove_role_from_user",
    "sync_roles_to_database",
]
