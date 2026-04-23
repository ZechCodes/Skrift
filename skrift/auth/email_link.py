"""Email-link challenge for deferred OAuth account linking.

Used when an OAuth provider returns a verified-looking email that already
belongs to another user, but the provider did not attest that the email is
verified. The caller (``skrift.controllers.auth``) stashes the pending link
state in the session, issues a short-lived signed URL, and delivers it via
the configured :class:`~skrift.lib.email_backends.EmailBackend`.

The signed URL payload binds to the originating session's ``pending_auth_id``
so that only the browser that started the OAuth flow can complete the link —
this blocks cross-browser / cross-user interception.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.oauth_account_service import EmailVerificationRequired
from skrift.auth.session_keys import (
    SESSION_PENDING_LINK_EMAIL,
    SESSION_PENDING_LINK_METADATA,
    SESSION_PENDING_LINK_TOKENS,
)
from skrift.auth.tokens import create_signed_token, verify_signed_token

if TYPE_CHECKING:
    from litestar import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from skrift.config import Settings
    from skrift.lib.email_backends import EmailBackend

EMAIL_LINK_PURPOSE = "oauth_link_verify"
EMAIL_LINK_TTL_SECONDS = 900  # 15 minutes


def _resolve_public_base_url(settings: "Settings") -> str:
    base = (settings.email.public_base_url or settings.auth.redirect_base_url or "").rstrip("/")
    return base


def _mask_email(email: str) -> str:
    try:
        local, _, domain = email.partition("@")
        if not local or not domain:
            return email
        if len(local) <= 2:
            masked_local = local[0] + "*"
        else:
            masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
        return f"{masked_local}@{domain}"
    except Exception:
        return email


def build_link_token(
    *,
    secret_key: str,
    pending_auth_id: str,
    identity: ResolvedPrimaryIdentity,
    existing_user_id: str,
) -> str:
    """Create a short-lived signed URL token for the email-link challenge."""
    payload = {
        "purpose": EMAIL_LINK_PURPOSE,
        "pending_auth_id": pending_auth_id,
        "provider_key": identity.method_key,
        "method_type": identity.method_type,
        "subject_id": identity.subject_id,
        "email": identity.email,
        "name": identity.name or "",
        "picture_url": identity.picture_url or "",
        "user_id_to_link": existing_user_id,
    }
    return create_signed_token(payload, secret_key, EMAIL_LINK_TTL_SECONDS)


def verify_link_token(token: str, secret_key: str) -> dict | None:
    """Verify a link token's signature, expiry, and purpose field.

    Revocation (reuse detection) must be checked separately via
    :func:`skrift.db.services.oauth2_service.is_token_revoked`.
    """
    payload = verify_signed_token(token, secret_key)
    if payload is None:
        return None
    if payload.get("purpose") != EMAIL_LINK_PURPOSE:
        return None
    return payload


def stash_pending_link_state(
    request: "Request",
    *,
    email: str,
    metadata: dict[str, Any] | None,
    tokens: dict[str, Any] | None,
) -> None:
    """Record raw provider metadata + tokens in session for later completion."""
    request.session[SESSION_PENDING_LINK_METADATA] = metadata or {}
    if tokens is not None:
        request.session[SESSION_PENDING_LINK_TOKENS] = tokens
    else:
        request.session.pop(SESSION_PENDING_LINK_TOKENS, None)
    request.session[SESSION_PENDING_LINK_EMAIL] = email


def clear_pending_link_state(request: "Request") -> None:
    request.session.pop(SESSION_PENDING_LINK_METADATA, None)
    request.session.pop(SESSION_PENDING_LINK_TOKENS, None)
    request.session.pop(SESSION_PENDING_LINK_EMAIL, None)


def get_pending_link_email(request: "Request") -> str | None:
    value = request.session.get(SESSION_PENDING_LINK_EMAIL)
    return str(value) if value else None


def get_pending_link_masked_email(request: "Request") -> str | None:
    email = get_pending_link_email(request)
    return _mask_email(email) if email else None


def pop_pending_link_metadata(request: "Request") -> dict[str, Any]:
    return dict(request.session.pop(SESSION_PENDING_LINK_METADATA, {}) or {})


def pop_pending_link_tokens(request: "Request") -> dict[str, Any] | None:
    value = request.session.pop(SESSION_PENDING_LINK_TOKENS, None)
    return dict(value) if value else None


def build_claim_url(settings: "Settings", token: str) -> str:
    """Compose the absolute URL that the email body embeds."""
    base = _resolve_public_base_url(settings)
    path = f"/auth/verify-email/claim/{token}"
    if not base:
        return path
    return urljoin(base + "/", path.lstrip("/"))


def expiry_from_payload(payload: dict) -> datetime:
    return datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)


async def send_link_challenge_email(
    email_backend: "EmailBackend",
    *,
    settings: "Settings",
    to: str,
    claim_url: str,
    provider_display: str,
    template_engine,
) -> None:
    """Render and dispatch the link-verification email."""
    context = {
        "claim_url": claim_url,
        "provider_display": provider_display,
        "ttl_minutes": EMAIL_LINK_TTL_SECONDS // 60,
        "to": to,
    }
    text_template = template_engine.get_template("auth/emails/link_verification.txt")
    html_template = template_engine.get_template("auth/emails/link_verification.html")
    text_body = text_template.render(context)
    html_body = html_template.render(context)

    subject = "Confirm sign-in to link your account"
    await email_backend.send_email(
        to=to,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


async def begin_email_link_challenge(
    request: "Request",
    *,
    settings: "Settings",
    email_backend: "EmailBackend",
    resolution: EmailVerificationRequired,
    pending_auth_id: str,
    template_engine,
    db_session: "AsyncSession | None" = None,  # reserved for future rate-limiter writes
) -> str:
    """Create the token, stash session state, and send the challenge email.

    Returns the claim URL that was emailed (useful for tests to follow the
    link without parsing outbound mail).
    """
    identity = resolution.identity
    token = build_link_token(
        secret_key=settings.secret_key,
        pending_auth_id=pending_auth_id,
        identity=identity,
        existing_user_id=resolution.existing_user_id,
    )
    claim_url = build_claim_url(settings, token)

    stash_pending_link_state(
        request,
        email=identity.email or "",
        metadata=dict(identity.raw_metadata or {}),
        tokens=resolution.tokens,
    )

    if identity.email:
        await send_link_challenge_email(
            email_backend,
            settings=settings,
            to=identity.email,
            claim_url=claim_url,
            provider_display=identity.method_key,
            template_engine=template_engine,
        )

    return claim_url
