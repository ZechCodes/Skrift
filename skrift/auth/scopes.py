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


# Registry of all scope definitions
SCOPE_DEFINITIONS: dict[str, ScopeDefinition] = {}


def register_scope(
    name: str,
    description: str,
    claims: list[str] | None = None,
    required: bool = False,
) -> ScopeDefinition:
    """Register a scope definition.

    Args:
        name: The scope identifier (e.g., "openid", "profile")
        description: Human-readable description of what this scope grants
        claims: List of claim names included when this scope is granted
        required: Whether the scope is the foundation the app's other scopes
            depend on — the consent screen renders it locked instead of
            user-declinable, and consent submission always grants it when
            it was requested

    Returns:
        The registered ScopeDefinition instance
    """
    scope = ScopeDefinition(
        name=name, description=description, claims=claims or [], required=required
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
