"""Authentication and authorization module."""

from skrift.auth.bearer import BearerJWTAuth, BearerJWTOnly
from skrift.auth.guards import (
    ADMINISTRATOR_PERMISSION,
    AndRequirement,
    AuthRequirement,
    OrRequirement,
    OwnerOrPermission,
    Permission,
    Role,
    auth_guard,
)
from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.methods import (
    PrimaryAuthCompletion,
    PrimaryAuthMethod,
    PrimaryAuthMethodDescriptor,
    get_primary_auth_method,
    register_primary_auth_method,
)
from skrift.auth.permissions import (
    ALLOW_ANONYMOUS_SERVICE,
    DISALLOW_API_GRANTS,
    REQUIRE_ELEVATED_SECURITY,
    REQUIRE_KNOWN_SERVICE,
    PERMISSION_DEFINITIONS,
    PermissionDefinition,
    ensure_permission,
    get_permission_definition,
    list_permission_definitions,
    register_permission,
)
from skrift.auth.second_factors import (
    SecondFactorMethod,
    SecondFactorMethodDescriptor,
    get_second_factor_method,
    register_second_factor_method,
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
from skrift.auth.session_service import finalize_authenticated_session
from skrift.auth.session_service import (
    PendingAuthTransitionDecision,
    PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
    PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
    PENDING_AUTH_TTL_SECONDS,
    apply_pending_authentication_transition,
    PendingAuthState,
    begin_pending_authentication,
    clear_pending_authentication,
    complete_pending_authentication,
    decide_pending_authentication_transition,
    get_pending_authentication,
    update_pending_authentication,
)

__all__ = [
    "ResolvedPrimaryIdentity",
    # Guards
    "ADMINISTRATOR_PERMISSION",
    "AndRequirement",
    "AuthRequirement",
    "BearerJWTAuth",
    "BearerJWTOnly",
    "OrRequirement",
    "OwnerOrPermission",
    "Permission",
    "Role",
    "auth_guard",
    # Primary auth methods
    "PrimaryAuthCompletion",
    "PrimaryAuthMethod",
    "PrimaryAuthMethodDescriptor",
    "get_primary_auth_method",
    "register_primary_auth_method",
    "ALLOW_ANONYMOUS_SERVICE",
    "DISALLOW_API_GRANTS",
    "REQUIRE_ELEVATED_SECURITY",
    "REQUIRE_KNOWN_SERVICE",
    "PERMISSION_DEFINITIONS",
    "PermissionDefinition",
    "ensure_permission",
    "get_permission_definition",
    "list_permission_definitions",
    "register_permission",
    # Second-factor methods
    "SecondFactorMethod",
    "SecondFactorMethodDescriptor",
    "get_second_factor_method",
    "register_second_factor_method",
    "PendingAuthState",
    "PendingAuthTransitionDecision",
    "PENDING_AUTH_STAGE_PRIMARY_VERIFIED",
    "PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED",
    "PENDING_AUTH_TTL_SECONDS",
    "begin_pending_authentication",
    "get_pending_authentication",
    "clear_pending_authentication",
    "update_pending_authentication",
    "complete_pending_authentication",
    "decide_pending_authentication_transition",
    "apply_pending_authentication_transition",
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
    "finalize_authenticated_session",
]
