"""OAuth2 scope definitions for the authorization server."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScopeDefinition:
    """Definition of an OAuth2 scope with its associated claims."""

    name: str
    description: str
    claims: list[str] = field(default_factory=list)
    required: bool = False
    sensitive: bool = False
    label: str | None = None
    details: str | None = None
    required_hint: str | None = None


# Registry of all scope definitions
SCOPE_DEFINITIONS: dict[str, ScopeDefinition] = {}


def register_scope(
    name: str,
    description: str,
    claims: list[str] | None = None,
    required: bool = False,
    sensitive: bool = False,
    label: str | None = None,
    details: str | None = None,
    required_hint: str | None = None,
) -> ScopeDefinition:
    """Register a scope definition.

    Args:
        name: The scope identifier (e.g., "openid", "profile")
        description: Human-readable one-line summary of what this scope grants;
            the consent screen's primary text when no ``label`` is given
        claims: List of claim names included when this scope is granted
        required: Whether the scope is the foundation the app's other scopes
            depend on — the consent screen renders it locked instead of
            user-declinable, and consent submission always grants it when
            it was requested
        sensitive: Whether the scope discloses authorization state and so must
            be granted to a client explicitly. A client with an empty
            ``allowed_scopes`` is otherwise treated as allowed every scope;
            a sensitive scope is refused for such a client instead of riding
            along on that wildcard
        label: Short display name for the scope (e.g. "View documents"); when
            given, the consent screen shows it as the heading with
            ``description`` as the summary line beneath
        details: Full explanation revealed behind a disclosure control on the
            consent screen; keep ``description`` short and put caveats here
        required_hint: Site-provided text rendered after the "Required" badge
            explaining why the scope cannot be declined

    Returns:
        The registered ScopeDefinition instance
    """
    scope = ScopeDefinition(
        name=name,
        description=description,
        claims=claims or [],
        required=required,
        sensitive=sensitive,
        label=label,
        details=details,
        required_hint=required_hint,
    )
    SCOPE_DEFINITIONS[scope.name] = scope
    return scope


def get_scope_definition(name: str) -> ScopeDefinition | None:
    """Get a scope definition by name."""
    return SCOPE_DEFINITIONS.get(name)


# Built-in scopes
register_scope("openid", "Verify your identity", claims=["sub"])
register_scope("profile", "Access your name and picture", claims=["name", "picture"])
register_scope("email", "Access your email address", claims=["email", "email_verified"])
register_scope(
    "groups",
    "Share which groups you belong to",
    claims=["groups"],
    sensitive=True,
    label="View your group memberships",
    details=(
        "Applications use this to decide what you are allowed to access. Only "
        "the names of your groups are shared — never the permissions they "
        "carry. Declining means an app that gates access on group membership "
        "will not let you in."
    ),
)
