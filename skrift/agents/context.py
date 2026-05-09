"""Actor context helpers for agent audit events."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

from skrift.agents.models import Actor

logger = logging.getLogger(__name__)
_actor: ContextVar[Actor | None] = ContextVar("skrift_agent_actor", default=None)
_current_session_id: ContextVar[str | None] = ContextVar(
    "skrift_agent_current_session_id",
    default=None,
)


def set_actor(actor: Actor | dict | str | None) -> None:
    """Set the current actor for subsequent Session operations."""

    _actor.set(coerce_actor(actor))


def coerce_actor(actor: Actor | dict | str | None) -> Actor:
    if isinstance(actor, Actor):
        return actor
    if isinstance(actor, dict):
        return Actor.model_validate(actor)
    if isinstance(actor, str):
        return Actor(kind="user", id=actor)
    return Actor()


def resolve_actor(actor: Actor | dict | str | None = None) -> Actor:
    if actor is not None:
        return coerce_actor(actor)
    current = _actor.get()
    if current is not None:
        return current
    logger.warning("Recording agent event with unknown actor")
    return Actor()


def current_session_id() -> str | None:
    """Return the session currently executing on this context, if any."""

    return _current_session_id.get()


def set_current_session_id(session_id: str) -> Token[str | None]:
    """Set the current executing session and return a reset token."""

    return _current_session_id.set(session_id)


def reset_current_session_id(token: Token[str | None]) -> None:
    """Reset the current executing session to a previous token."""

    _current_session_id.reset(token)
