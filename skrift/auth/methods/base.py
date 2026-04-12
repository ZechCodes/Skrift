"""Primary authentication method interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from skrift.auth.identities import ResolvedPrimaryIdentity


@dataclass(frozen=True, slots=True)
class PrimaryAuthMethodDescriptor:
    """Presentation metadata for a login method."""

    key: str
    method_type: str
    name: str
    icon: str
    start_path: str


@dataclass(slots=True)
class PrimaryAuthCompletion:
    """Identity payload returned when a method completes primary auth."""

    identity: ResolvedPrimaryIdentity
    raw_user_info: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)


class PrimaryAuthMethod(ABC):
    """Base class for pluggable primary authentication methods."""

    method_type: str

    def __init__(self, method_key: str):
        self.method_key = method_key

    @abstractmethod
    def get_descriptor(self) -> PrimaryAuthMethodDescriptor:
        """Return presentation metadata for the login page."""
        ...

    @abstractmethod
    async def begin_auth(self, request, *, next_url: str | None = None):
        """Start this method's authentication flow."""
        ...

    @abstractmethod
    async def complete_auth(self, request, **kwargs) -> PrimaryAuthCompletion:
        """Complete this method's authentication flow."""
        ...
