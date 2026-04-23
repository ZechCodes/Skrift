"""Passkey registration and assertion helpers.

Attestation policy
------------------

Registration calls pass no ``attestation`` argument to
``webauthn.generate_registration_options``, which means the library
default — ``"none"`` — is used. The authenticator does not return its
hardware attestation statement, and we don't verify an attestation
certificate chain. This is the right default for a consumer CMS:

- It preserves user privacy (``"direct"`` attestation sends an
  authenticator-specific identifier that can correlate users across
  relying parties).
- It simplifies enrollment (no AAGUID allowlist or metadata service).
- Every `complete_*` still sets ``require_user_verification=True``, so
  enrollment + authentication always require a user-present gesture
  (biometric / PIN).

If you need hardware attestation — e.g. enterprise enrollment that
only accepts FIDO-certified authenticators — the knob is
``attestation="direct"`` on ``generate_registration_options`` plus
server-side chain verification against an AAGUID allowlist. That is
out of scope for the current deployment target.
"""

from __future__ import annotations

import base64
import importlib
import json
from dataclasses import dataclass
from time import time
from urllib.parse import urlparse
from uuid import uuid4

from skrift.auth.session_keys import (
    SESSION_PASSKEY_AUTHENTICATION_CHALLENGE,
    SESSION_PASSKEY_AUTHENTICATION_EXPIRES_AT,
    SESSION_PASSKEY_AUTHENTICATION_PENDING_AUTH_ID,
    SESSION_PASSKEY_AUTHENTICATION_USER_ID,
    SESSION_PASSKEY_PRIMARY_AUTH_CHALLENGE,
    SESSION_PASSKEY_PRIMARY_AUTH_EXPIRES_AT,
    SESSION_PASSKEY_PRIMARY_AUTH_METHOD,
    SESSION_PASSKEY_PRIMARY_REGISTRATION_CHALLENGE,
    SESSION_PASSKEY_PRIMARY_REGISTRATION_EMAIL,
    SESSION_PASSKEY_PRIMARY_REGISTRATION_EXPIRES_AT,
    SESSION_PASSKEY_PRIMARY_REGISTRATION_METHOD,
    SESSION_PASSKEY_PRIMARY_REGISTRATION_NAME,
    SESSION_PASSKEY_PRIMARY_REGISTRATION_USER_HANDLE,
    SESSION_PASSKEY_REGISTRATION_CHALLENGE,
    SESSION_PASSKEY_REGISTRATION_EXPIRES_AT,
    SESSION_PASSKEY_REGISTRATION_USER_ID,
)


# WebAuthn challenges are short-lived: five minutes is comfortably above any
# normal user-interaction time (prompt + authenticator + round trip) but
# cuts off "challenge mined from a stale long-lived session" replay.
PASSKEY_CHALLENGE_TTL_SECONDS = 300


class PasskeyRuntimeUnavailableError(RuntimeError):
    """Raised when the optional WebAuthn runtime dependency is unavailable."""


class PasskeyStateError(RuntimeError):
    """Raised when the current session lacks required passkey state."""


class PasskeyVerificationError(ValueError):
    """Raised when a passkey credential fails verification."""


@dataclass(frozen=True, slots=True)
class PasskeyRegistrationResult:
    """Normalized registration result for persistence."""

    credential_id: str
    public_key: str
    sign_count: int
    transports: list[str]
    enrollment_metadata: dict


@dataclass(frozen=True, slots=True)
class PasskeyAuthenticationResult:
    """Normalized assertion verification result."""

    credential_id: str
    new_sign_count: int
    verification_metadata: dict


@dataclass(frozen=True, slots=True)
class PrimaryPasskeyRegistrationState:
    """Session-backed state for primary passkey signup."""

    method_key: str
    email: str
    name: str | None
    user_handle: str


def _bytes_to_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _challenge_expiry() -> int:
    """Seconds-since-epoch deadline for a newly-issued WebAuthn challenge."""
    return int(time()) + PASSKEY_CHALLENGE_TTL_SECONDS


def _require_unexpired_challenge(request, expires_at_key: str) -> None:
    """Raise :class:`PasskeyStateError` when the challenge deadline is past.

    Checked at the top of each ``complete_*`` before the webauthn verify
    call so stale session state is rejected without wasting a crypto
    verification cycle. Missing timestamps are treated as expired — the
    ``begin_*`` helper always stamps one, so an absent key means the
    session's passkey state is incomplete or was hand-edited.
    """
    expires_at = request.session.get(expires_at_key)
    if not isinstance(expires_at, (int, float)) or int(expires_at) <= int(time()):
        raise PasskeyStateError("Passkey challenge expired or missing")


def _resolve_origin(request, settings) -> str:
    """Resolve the WebAuthn origin from trusted configuration only.

    A spoofed Host header must not be able to steer registrations or
    assertions to an attacker-controlled origin, so we refuse to fall back
    to ``request.base_url``. Deployments that enable passkey methods must
    configure ``auth.redirect_base_url`` (or rely on the startup check in
    :func:`validate_passkey_origin_config`).
    """
    origin = (settings.auth.redirect_base_url or "").rstrip("/")
    if not origin:
        raise PasskeyStateError(
            "Passkey origin is not configured: set 'auth.redirect_base_url' "
            "in your app config"
        )
    return origin


def _resolve_rp_id(request, settings) -> str:
    """Resolve the WebAuthn Relying Party ID.

    Only reads from trusted configuration. The previous fallback to
    ``request.url.hostname`` has been removed because a spoofed Host header
    would otherwise let an attacker pick the RP ID.
    """
    if settings.domain:
        return settings.domain.strip()
    origin = _resolve_origin(request, settings)
    parsed = urlparse(origin)
    if parsed.hostname:
        return parsed.hostname
    raise PasskeyStateError(
        "Unable to determine WebAuthn relying party ID: set 'domain' or "
        "'auth.redirect_base_url' in your app config"
    )


def validate_passkey_origin_config(settings) -> None:
    """Fail fast at startup if passkey methods are enabled without a pinned origin.

    Call from the app factory after settings are loaded. Raises
    :class:`PasskeyStateError` when neither ``settings.domain`` nor
    ``settings.auth.redirect_base_url`` supplies a hostname.
    """
    if settings.domain and settings.domain.strip():
        return
    origin = (settings.auth.redirect_base_url or "").rstrip("/")
    if origin:
        parsed = urlparse(origin)
        if parsed.hostname:
            return
    raise PasskeyStateError(
        "Passkey authentication requires a pinned relying-party hostname. "
        "Set 'domain' or a full 'auth.redirect_base_url' in your app config."
    )


def _resolve_rp_name(settings) -> str:
    return settings.domain.strip() or "Skrift"


def is_webauthn_available() -> bool:
    """Return True when the optional WebAuthn dependency is importable."""
    return importlib.util.find_spec("webauthn") is not None


def _load_webauthn_symbols() -> dict[str, object]:
    if not is_webauthn_available():
        raise PasskeyRuntimeUnavailableError(
            "Passkey support requires the optional 'webauthn' dependency"
        )

    from webauthn import (
        generate_authentication_options,
        generate_registration_options,
        options_to_json,
        verify_authentication_response,
        verify_registration_response,
    )
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    return {
        "base64url_to_bytes": base64url_to_bytes,
        "generate_authentication_options": generate_authentication_options,
        "generate_registration_options": generate_registration_options,
        "options_to_json": options_to_json,
        "PublicKeyCredentialDescriptor": PublicKeyCredentialDescriptor,
        "UserVerificationRequirement": UserVerificationRequirement,
        "verify_authentication_response": verify_authentication_response,
        "verify_registration_response": verify_registration_response,
    }


def clear_passkey_registration_state(request) -> None:
    """Clear registration challenge state from the session."""
    request.session.pop(SESSION_PASSKEY_REGISTRATION_CHALLENGE, None)
    request.session.pop(SESSION_PASSKEY_REGISTRATION_USER_ID, None)
    request.session.pop(SESSION_PASSKEY_REGISTRATION_EXPIRES_AT, None)


def clear_passkey_authentication_state(request) -> None:
    """Clear authentication challenge state from the session."""
    request.session.pop(SESSION_PASSKEY_AUTHENTICATION_CHALLENGE, None)
    request.session.pop(SESSION_PASSKEY_AUTHENTICATION_USER_ID, None)
    request.session.pop(SESSION_PASSKEY_AUTHENTICATION_PENDING_AUTH_ID, None)
    request.session.pop(SESSION_PASSKEY_AUTHENTICATION_EXPIRES_AT, None)


def clear_primary_passkey_authentication_state(request) -> None:
    """Clear primary-auth passkey challenge state from the session."""
    request.session.pop(SESSION_PASSKEY_PRIMARY_AUTH_CHALLENGE, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_AUTH_METHOD, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_AUTH_EXPIRES_AT, None)


def clear_primary_passkey_registration_state(request) -> None:
    """Clear primary-auth passkey registration state from the session."""
    request.session.pop(SESSION_PASSKEY_PRIMARY_REGISTRATION_CHALLENGE, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_REGISTRATION_METHOD, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_REGISTRATION_EMAIL, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_REGISTRATION_NAME, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_REGISTRATION_USER_HANDLE, None)
    request.session.pop(SESSION_PASSKEY_PRIMARY_REGISTRATION_EXPIRES_AT, None)


def get_primary_passkey_registration_state(request) -> PrimaryPasskeyRegistrationState | None:
    """Return the current primary passkey registration state."""
    challenge = request.session.get(SESSION_PASSKEY_PRIMARY_REGISTRATION_CHALLENGE)
    method_key = request.session.get(SESSION_PASSKEY_PRIMARY_REGISTRATION_METHOD)
    email = request.session.get(SESSION_PASSKEY_PRIMARY_REGISTRATION_EMAIL)
    user_handle = request.session.get(SESSION_PASSKEY_PRIMARY_REGISTRATION_USER_HANDLE)
    if not challenge or not method_key or not email or not user_handle:
        return None

    name = request.session.get(SESSION_PASSKEY_PRIMARY_REGISTRATION_NAME)
    return PrimaryPasskeyRegistrationState(
        method_key=str(method_key),
        email=str(email),
        name=str(name) if name else None,
        user_handle=str(user_handle),
    )


def begin_passkey_registration(request, settings, user, enrollments) -> dict:
    """Create passkey registration options and persist the challenge in session."""
    symbols = _load_webauthn_symbols()
    descriptor_class = symbols["PublicKeyCredentialDescriptor"]
    base64url_to_bytes = symbols["base64url_to_bytes"]
    generate_registration_options = symbols["generate_registration_options"]
    options_to_json = symbols["options_to_json"]

    exclude_credentials = [
        descriptor_class(id=base64url_to_bytes(enrollment.credential_id))
        for enrollment in enrollments
        if enrollment.credential_id
    ]
    options = generate_registration_options(
        rp_id=_resolve_rp_id(request, settings),
        rp_name=_resolve_rp_name(settings),
        user_id=user.id.bytes if hasattr(user.id, "bytes") else str(user.id).encode("utf-8"),
        user_name=user.email or f"user-{user.id}",
        user_display_name=user.name or user.email or "User",
        exclude_credentials=exclude_credentials,
    )
    serialized = json.loads(options_to_json(options))

    request.session[SESSION_PASSKEY_REGISTRATION_CHALLENGE] = serialized["challenge"]
    request.session[SESSION_PASSKEY_REGISTRATION_USER_ID] = str(user.id)
    request.session[SESSION_PASSKEY_REGISTRATION_EXPIRES_AT] = _challenge_expiry()
    return serialized


def complete_passkey_registration(request, settings, user, credential) -> PasskeyRegistrationResult:
    """Verify a registration credential against the session challenge."""
    _require_unexpired_challenge(request, SESSION_PASSKEY_REGISTRATION_EXPIRES_AT)

    symbols = _load_webauthn_symbols()
    base64url_to_bytes = symbols["base64url_to_bytes"]
    verify_registration_response = symbols["verify_registration_response"]

    expected_user_id = request.session.get(SESSION_PASSKEY_REGISTRATION_USER_ID)
    expected_challenge = request.session.get(SESSION_PASSKEY_REGISTRATION_CHALLENGE)
    if not expected_challenge or not expected_user_id or expected_user_id != str(user.id):
        raise PasskeyStateError("Passkey registration session is missing or invalid")

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_origin=_resolve_origin(request, settings),
            expected_rp_id=_resolve_rp_id(request, settings),
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyVerificationError(str(exc)) from exc
    finally:
        clear_passkey_registration_state(request)

    transports = credential.get("response", {}).get("transports", []) or []
    metadata = {
        "credential_device_type": getattr(verification, "credential_device_type", ""),
        "credential_backed_up": bool(getattr(verification, "credential_backed_up", False)),
        "fmt": credential.get("response", {}).get("publicKeyAlgorithm"),
    }

    return PasskeyRegistrationResult(
        credential_id=_bytes_to_base64url(verification.credential_id),
        public_key=_bytes_to_base64url(verification.credential_public_key),
        sign_count=int(getattr(verification, "sign_count", 0) or 0),
        transports=[transport for transport in transports if transport],
        enrollment_metadata=metadata,
    )


def begin_passkey_authentication(request, settings, user, pending_auth, enrollments) -> dict:
    """Create authentication options and persist the challenge in session."""
    symbols = _load_webauthn_symbols()
    descriptor_class = symbols["PublicKeyCredentialDescriptor"]
    base64url_to_bytes = symbols["base64url_to_bytes"]
    generate_authentication_options = symbols["generate_authentication_options"]
    options_to_json = symbols["options_to_json"]
    user_verification_requirement = symbols["UserVerificationRequirement"]

    allow_credentials = [
        descriptor_class(id=base64url_to_bytes(enrollment.credential_id))
        for enrollment in enrollments
        if enrollment.credential_id
    ]
    options = generate_authentication_options(
        rp_id=_resolve_rp_id(request, settings),
        allow_credentials=allow_credentials,
        user_verification=user_verification_requirement.REQUIRED,
    )
    serialized = json.loads(options_to_json(options))

    request.session[SESSION_PASSKEY_AUTHENTICATION_CHALLENGE] = serialized["challenge"]
    request.session[SESSION_PASSKEY_AUTHENTICATION_USER_ID] = str(user.id)
    request.session[SESSION_PASSKEY_AUTHENTICATION_PENDING_AUTH_ID] = pending_auth.pending_auth_id
    request.session[SESSION_PASSKEY_AUTHENTICATION_EXPIRES_AT] = _challenge_expiry()
    return serialized


def begin_primary_passkey_authentication(request, settings, method_key: str) -> dict:
    """Create authentication options for a primary passkey sign-in."""
    symbols = _load_webauthn_symbols()
    generate_authentication_options = symbols["generate_authentication_options"]
    options_to_json = symbols["options_to_json"]
    user_verification_requirement = symbols["UserVerificationRequirement"]

    options = generate_authentication_options(
        rp_id=_resolve_rp_id(request, settings),
        user_verification=user_verification_requirement.REQUIRED,
    )
    serialized = json.loads(options_to_json(options))

    request.session[SESSION_PASSKEY_PRIMARY_AUTH_CHALLENGE] = serialized["challenge"]
    request.session[SESSION_PASSKEY_PRIMARY_AUTH_METHOD] = method_key
    request.session[SESSION_PASSKEY_PRIMARY_AUTH_EXPIRES_AT] = _challenge_expiry()
    return serialized


def begin_primary_passkey_registration(
    request,
    settings,
    *,
    method_key: str,
    email: str,
    name: str | None = None,
) -> dict:
    """Create registration options for primary passkey signup."""
    symbols = _load_webauthn_symbols()
    generate_registration_options = symbols["generate_registration_options"]
    options_to_json = symbols["options_to_json"]

    # Throwaway per-credential user handle, deliberately unlinked from
    # ``User.id``: WebAuthn §5.1.3 recommends random values here so the
    # relying party does not leak a stable internal identifier across
    # credentials. Do NOT swap this for ``user.id`` — we have no user row
    # at this point anyway (signup happens after verification).
    user_handle = uuid4().hex
    options = generate_registration_options(
        rp_id=_resolve_rp_id(request, settings),
        rp_name=_resolve_rp_name(settings),
        user_id=user_handle.encode("utf-8"),
        user_name=email,
        user_display_name=name or email,
    )
    serialized = json.loads(options_to_json(options))

    request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_CHALLENGE] = serialized["challenge"]
    request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_METHOD] = method_key
    request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_EMAIL] = email
    request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_NAME] = name or ""
    request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_USER_HANDLE] = user_handle
    request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_EXPIRES_AT] = _challenge_expiry()
    return serialized


def complete_passkey_authentication(
    request,
    settings,
    user,
    pending_auth,
    enrollment,
    credential,
) -> PasskeyAuthenticationResult:
    """Verify a passkey assertion against an enrolled credential."""
    _require_unexpired_challenge(request, SESSION_PASSKEY_AUTHENTICATION_EXPIRES_AT)

    symbols = _load_webauthn_symbols()
    base64url_to_bytes = symbols["base64url_to_bytes"]
    verify_authentication_response = symbols["verify_authentication_response"]

    expected_user_id = request.session.get(SESSION_PASSKEY_AUTHENTICATION_USER_ID)
    expected_challenge = request.session.get(SESSION_PASSKEY_AUTHENTICATION_CHALLENGE)
    expected_pending_auth_id = request.session.get(SESSION_PASSKEY_AUTHENTICATION_PENDING_AUTH_ID)
    if (
        not expected_challenge
        or expected_user_id != str(user.id)
        or expected_pending_auth_id != pending_auth.pending_auth_id
    ):
        raise PasskeyStateError("Passkey authentication session is missing or invalid")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_origin=_resolve_origin(request, settings),
            expected_rp_id=_resolve_rp_id(request, settings),
            credential_public_key=base64url_to_bytes(enrollment.public_key),
            credential_current_sign_count=enrollment.sign_count or 0,
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyVerificationError(str(exc)) from exc
    finally:
        clear_passkey_authentication_state(request)

    return PasskeyAuthenticationResult(
        credential_id=enrollment.credential_id or credential.get("id", ""),
        new_sign_count=int(getattr(verification, "new_sign_count", 0) or 0),
        verification_metadata={
            "credential_device_type": getattr(verification, "credential_device_type", ""),
            "credential_backed_up": bool(getattr(verification, "credential_backed_up", False)),
            "user_verified": bool(getattr(verification, "user_verified", True)),
        },
    )


def complete_primary_passkey_authentication(
    request,
    settings,
    *,
    method_key: str,
    enrollment,
    credential,
) -> PasskeyAuthenticationResult:
    """Verify a passkey assertion for primary sign-in."""
    _require_unexpired_challenge(request, SESSION_PASSKEY_PRIMARY_AUTH_EXPIRES_AT)

    symbols = _load_webauthn_symbols()
    base64url_to_bytes = symbols["base64url_to_bytes"]
    verify_authentication_response = symbols["verify_authentication_response"]

    expected_challenge = request.session.get(SESSION_PASSKEY_PRIMARY_AUTH_CHALLENGE)
    expected_method_key = request.session.get(SESSION_PASSKEY_PRIMARY_AUTH_METHOD)
    if not expected_challenge or expected_method_key != method_key:
        raise PasskeyStateError("Primary passkey authentication session is missing or invalid")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_origin=_resolve_origin(request, settings),
            expected_rp_id=_resolve_rp_id(request, settings),
            credential_public_key=base64url_to_bytes(enrollment.public_key),
            credential_current_sign_count=enrollment.sign_count or 0,
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyVerificationError(str(exc)) from exc
    finally:
        clear_primary_passkey_authentication_state(request)

    return PasskeyAuthenticationResult(
        credential_id=enrollment.credential_id or credential.get("id", ""),
        new_sign_count=int(getattr(verification, "new_sign_count", 0) or 0),
        verification_metadata={
            "credential_device_type": getattr(verification, "credential_device_type", ""),
            "credential_backed_up": bool(getattr(verification, "credential_backed_up", False)),
            "user_verified": bool(getattr(verification, "user_verified", True)),
        },
    )


def complete_primary_passkey_registration(
    request,
    settings,
    *,
    method_key: str,
    credential,
) -> PasskeyRegistrationResult:
    """Verify a primary passkey signup registration response."""
    _require_unexpired_challenge(request, SESSION_PASSKEY_PRIMARY_REGISTRATION_EXPIRES_AT)

    state = get_primary_passkey_registration_state(request)
    if state is None or state.method_key != method_key:
        raise PasskeyStateError("Primary passkey registration session is missing or invalid")

    symbols = _load_webauthn_symbols()
    base64url_to_bytes = symbols["base64url_to_bytes"]
    verify_registration_response = symbols["verify_registration_response"]

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(
                request.session[SESSION_PASSKEY_PRIMARY_REGISTRATION_CHALLENGE]
            ),
            expected_origin=_resolve_origin(request, settings),
            expected_rp_id=_resolve_rp_id(request, settings),
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyVerificationError(str(exc)) from exc
    finally:
        clear_primary_passkey_registration_state(request)

    transports = credential.get("response", {}).get("transports", []) or []
    metadata = {
        "credential_device_type": getattr(verification, "credential_device_type", ""),
        "credential_backed_up": bool(getattr(verification, "credential_backed_up", False)),
        "fmt": credential.get("response", {}).get("publicKeyAlgorithm"),
    }
    return PasskeyRegistrationResult(
        credential_id=_bytes_to_base64url(verification.credential_id),
        public_key=_bytes_to_base64url(verification.credential_public_key),
        sign_count=int(getattr(verification, "sign_count", 0) or 0),
        transports=[transport for transport in transports if transport],
        enrollment_metadata=metadata,
    )
