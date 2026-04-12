"""Helpers for pending-auth and authenticated session transitions."""

from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from litestar import Request

from skrift.auth.session_keys import (
    SESSION_AUTH_NEXT,
    SESSION_PENDING_AUTH_EMAIL,
    SESSION_PENDING_AUTH_EXPIRES_AT,
    SESSION_PENDING_AUTH_ID,
    SESSION_PENDING_AUTH_IS_NEW_USER,
    SESSION_PENDING_AUTH_METHOD,
    SESSION_PENDING_AUTH_METHOD_TYPE,
    SESSION_PENDING_AUTH_NAME,
    SESSION_PENDING_AUTH_STAGE,
    SESSION_PENDING_AUTH_SUBJECT_ID,
    SESSION_PENDING_AUTH_USER_ID,
    SESSION_USER_EMAIL,
    SESSION_USER_ID,
    SESSION_USER_NAME,
    SESSION_USER_PICTURE_URL,
)

if TYPE_CHECKING:
    from skrift.auth.identities import ResolvedPrimaryIdentity

PENDING_AUTH_TTL_SECONDS = 900
PENDING_AUTH_STAGE_PRIMARY_VERIFIED = "primary_verified"
PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED = "second_factor_required"

_PENDING_AUTH_SESSION_KEYS = (
    SESSION_PENDING_AUTH_ID,
    SESSION_PENDING_AUTH_USER_ID,
    SESSION_PENDING_AUTH_METHOD,
    SESSION_PENDING_AUTH_METHOD_TYPE,
    SESSION_PENDING_AUTH_STAGE,
    SESSION_PENDING_AUTH_SUBJECT_ID,
    SESSION_PENDING_AUTH_EMAIL,
    SESSION_PENDING_AUTH_NAME,
    SESSION_PENDING_AUTH_IS_NEW_USER,
    SESSION_PENDING_AUTH_EXPIRES_AT,
)


@dataclass(slots=True)
class PendingAuthState:
    """Session-backed state for a partially completed authentication flow."""

    pending_auth_id: str
    method_key: str
    method_type: str
    stage: str
    user_id: str | None = None
    subject_id: str | None = None
    email: str | None = None
    name: str | None = None
    is_new_user: bool = False
    expires_at: int = 0

    @property
    def is_expired(self) -> bool:
        """Return True when the pending auth session has expired."""
        return self.expires_at <= int(time())


@dataclass(slots=True)
class PendingAuthTransitionDecision:
    """Decision describing what happens after primary auth succeeds."""

    promote_immediately: bool = True
    next_url: str | None = None
    stage: str | None = None


def _clear_pending_auth_keys(request: Request) -> None:
    """Remove all pending-auth keys from the current session."""
    for key in _PENDING_AUTH_SESSION_KEYS:
        request.session.pop(key, None)


def begin_pending_authentication(
    request: Request,
    *,
    method_key: str,
    method_type: str,
    identity: ResolvedPrimaryIdentity | None = None,
    user_id: str | None = None,
    is_new_user: bool = False,
    stage: str = PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
    ttl_seconds: int = PENDING_AUTH_TTL_SECONDS,
) -> PendingAuthState:
    """Start a pending-auth session after primary auth succeeds."""
    pending_auth = PendingAuthState(
        pending_auth_id=uuid4().hex,
        method_key=method_key,
        method_type=method_type,
        stage=stage,
        user_id=user_id,
        subject_id=identity.subject_id if identity else None,
        email=identity.email if identity else None,
        name=identity.name if identity else None,
        is_new_user=is_new_user,
        expires_at=int(time()) + ttl_seconds,
    )

    request.session[SESSION_PENDING_AUTH_ID] = pending_auth.pending_auth_id
    request.session[SESSION_PENDING_AUTH_METHOD] = pending_auth.method_key
    request.session[SESSION_PENDING_AUTH_METHOD_TYPE] = pending_auth.method_type
    request.session[SESSION_PENDING_AUTH_STAGE] = pending_auth.stage
    request.session[SESSION_PENDING_AUTH_EXPIRES_AT] = pending_auth.expires_at

    if pending_auth.user_id is not None:
        request.session[SESSION_PENDING_AUTH_USER_ID] = pending_auth.user_id
    if pending_auth.subject_id is not None:
        request.session[SESSION_PENDING_AUTH_SUBJECT_ID] = pending_auth.subject_id
    if pending_auth.email is not None:
        request.session[SESSION_PENDING_AUTH_EMAIL] = pending_auth.email
    if pending_auth.name is not None:
        request.session[SESSION_PENDING_AUTH_NAME] = pending_auth.name
    request.session[SESSION_PENDING_AUTH_IS_NEW_USER] = pending_auth.is_new_user

    return pending_auth


def get_pending_authentication(request: Request) -> PendingAuthState | None:
    """Return the pending-auth state from the session if present and valid."""
    pending_auth_id = request.session.get(SESSION_PENDING_AUTH_ID)
    method_key = request.session.get(SESSION_PENDING_AUTH_METHOD)
    method_type = request.session.get(SESSION_PENDING_AUTH_METHOD_TYPE)
    stage = request.session.get(SESSION_PENDING_AUTH_STAGE)
    expires_at = request.session.get(SESSION_PENDING_AUTH_EXPIRES_AT)

    if not pending_auth_id or not method_key or not method_type or not stage or expires_at is None:
        return None

    pending_auth = PendingAuthState(
        pending_auth_id=str(pending_auth_id),
        method_key=str(method_key),
        method_type=str(method_type),
        stage=str(stage),
        user_id=str(request.session.get(SESSION_PENDING_AUTH_USER_ID))
        if request.session.get(SESSION_PENDING_AUTH_USER_ID) is not None
        else None,
        subject_id=str(request.session.get(SESSION_PENDING_AUTH_SUBJECT_ID))
        if request.session.get(SESSION_PENDING_AUTH_SUBJECT_ID) is not None
        else None,
        email=str(request.session.get(SESSION_PENDING_AUTH_EMAIL))
        if request.session.get(SESSION_PENDING_AUTH_EMAIL) is not None
        else None,
        name=str(request.session.get(SESSION_PENDING_AUTH_NAME))
        if request.session.get(SESSION_PENDING_AUTH_NAME) is not None
        else None,
        is_new_user=bool(request.session.get(SESSION_PENDING_AUTH_IS_NEW_USER, False)),
        expires_at=int(expires_at),
    )

    if pending_auth.is_expired:
        clear_pending_authentication(request)
        return None

    return pending_auth


def clear_pending_authentication(request: Request) -> None:
    """Clear any pending-auth state from the session."""
    _clear_pending_auth_keys(request)


def update_pending_authentication(
    request: Request,
    *,
    stage: str | None = None,
    ttl_seconds: int | None = None,
) -> PendingAuthState:
    """Update the current pending-auth session and return the new state."""
    pending_auth = get_pending_authentication(request)
    if pending_auth is None:
        raise ValueError("No pending authentication session found")

    if stage is not None:
        pending_auth.stage = stage
        request.session[SESSION_PENDING_AUTH_STAGE] = stage

    if ttl_seconds is not None:
        pending_auth.expires_at = int(time()) + ttl_seconds
        request.session[SESSION_PENDING_AUTH_EXPIRES_AT] = pending_auth.expires_at

    return pending_auth


def finalize_authenticated_session(request: Request, user) -> None:
    """Rotate the session and populate it with authenticated user data."""
    flash = request.session.get("flash")
    flash_messages = request.session.get("flash_messages")
    nid = request.session.get("_nid")
    auth_next = request.session.get(SESSION_AUTH_NEXT)

    request.session.clear()

    request.session[SESSION_USER_ID] = str(user.id)
    request.session[SESSION_USER_NAME] = user.name
    request.session[SESSION_USER_EMAIL] = user.email
    request.session[SESSION_USER_PICTURE_URL] = user.picture_url

    if flash is not None:
        request.session["flash"] = flash
    if flash_messages is not None:
        request.session["flash_messages"] = flash_messages
    if nid is not None:
        request.session["_nid"] = nid
    if auth_next is not None:
        request.session[SESSION_AUTH_NEXT] = auth_next


def complete_pending_authentication(
    request: Request,
    user,
    *,
    pending_auth: PendingAuthState | None = None,
) -> None:
    """Promote a pending-auth session into a fully authenticated session."""
    pending_auth = pending_auth or get_pending_authentication(request)
    if pending_auth is None:
        raise ValueError("No pending authentication session found")

    clear_pending_authentication(request)
    finalize_authenticated_session(request, user)


async def decide_pending_authentication_transition(
    request: Request,
    login_result: Any,
    pending_auth: PendingAuthState,
    *,
    initial_decision: PendingAuthTransitionDecision | None = None,
) -> PendingAuthTransitionDecision:
    """Decide whether pending auth should promote immediately or remain pending."""
    from skrift.lib.hooks import AUTH_PENDING_AUTHENTICATION, hooks

    decision = initial_decision or PendingAuthTransitionDecision(promote_immediately=True)
    decision = await hooks.apply_filters(
        AUTH_PENDING_AUTHENTICATION,
        decision,
        login_result,
        pending_auth,
        request,
    )
    if not isinstance(decision, PendingAuthTransitionDecision):
        raise TypeError("Pending auth transition filter must return PendingAuthTransitionDecision")
    return decision


async def apply_pending_authentication_transition(
    request: Request,
    user,
    *,
    login_result: Any,
    pending_auth: PendingAuthState,
    initial_decision: PendingAuthTransitionDecision | None = None,
) -> PendingAuthTransitionDecision:
    """Apply the pending-auth transition decision to the current session."""
    decision = await decide_pending_authentication_transition(
        request,
        login_result,
        pending_auth,
        initial_decision=initial_decision,
    )
    if decision.promote_immediately:
        complete_pending_authentication(request, user, pending_auth=pending_auth)
        return decision

    if decision.stage is not None and decision.stage != pending_auth.stage:
        update_pending_authentication(request, stage=decision.stage)

    if decision.next_url is None:
        raise ValueError(
            "Pending auth transition decision must provide next_url when promotion is deferred"
        )

    return decision
