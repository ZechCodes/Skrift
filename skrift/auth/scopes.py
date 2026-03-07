"""OAuth2 scope definitions for the authorization server."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScopeDefinition:
    """Definition of an OAuth2 scope with its associated claims."""

    name: str
    description: str
    claims: list[str] = field(default_factory=list)


# Registry of all scope definitions
SCOPE_DEFINITIONS: dict[str, ScopeDefinition] = {}


def register_scope(name: str, description: str, claims: list[str] | None = None) -> ScopeDefinition:
    """Register a scope definition.

    Args:
        name: The scope identifier (e.g., "openid", "profile")
        description: Human-readable description of what this scope grants
        claims: List of claim names included when this scope is granted

    Returns:
        The registered ScopeDefinition instance
    """
    scope = ScopeDefinition(name=name, description=description, claims=claims or [])
    SCOPE_DEFINITIONS[scope.name] = scope
    return scope


def get_scope_definition(name: str) -> ScopeDefinition | None:
    """Get a scope definition by name."""
    return SCOPE_DEFINITIONS.get(name)


# Built-in scopes
register_scope("openid", "Verify your identity", claims=["sub"])
register_scope("profile", "Access your name and picture", claims=["name", "picture"])
register_scope("email", "Access your email address", claims=["email"])
