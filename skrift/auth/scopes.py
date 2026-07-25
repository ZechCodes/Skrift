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
