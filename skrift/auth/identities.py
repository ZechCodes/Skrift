"""Generic primary-auth identity models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skrift.auth.providers import NormalizedUserData


@dataclass(slots=True)
class ResolvedPrimaryIdentity:
    """Normalized identity returned by a primary authentication method."""

    method_key: str
    method_type: str
    subject_id: str
    email: str | None
    name: str | None
    picture_url: str | None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    provided_fields: set[str] = field(default_factory=set)
    can_create_account: bool = True
    # ``True`` only when the upstream method (OAuth provider, magic-link
    # challenge, etc.) attested that this email address is controlled by the
    # subject. Gates account auto-linking when an email match is found — see
    # :func:`skrift.auth.oauth_account_service.find_or_create_user_for_identity`.
    email_verified: bool = False


def identity_from_oauth_user_data(
    method_key: str,
    method_type: str,
    user_data: NormalizedUserData,
    raw_metadata: dict[str, Any],
) -> ResolvedPrimaryIdentity:
    """Build a generic primary-auth identity from normalized OAuth user data."""
    provided_fields = {
        field_name
        for field_name, value in (
            ("email", user_data.email),
            ("name", user_data.name),
            ("picture_url", user_data.picture_url),
        )
        if value
    }
    return ResolvedPrimaryIdentity(
        method_key=method_key,
        method_type=method_type,
        subject_id=user_data.oauth_id or "",
        email=user_data.email,
        name=user_data.name,
        picture_url=user_data.picture_url,
        raw_metadata=raw_metadata,
        provided_fields=provided_fields,
        email_verified=user_data.email_verified,
    )
