"""Second-factor enrollment and policy services."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from skrift.auth.second_factors.base import SecondFactorMethodDescriptor
from skrift.auth.second_factors.registry import get_second_factor_method
from skrift.auth.session_service import (
    PENDING_AUTH_STAGE_PRIMARY_VERIFIED,
    PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
    PendingAuthState,
    PendingAuthTransitionDecision,
)
from skrift.db.models.second_factor import SecondFactorEnrollment


async def list_second_factor_enrollments(
    db_session,
    user_id: str,
    *,
    active_only: bool = True,
) -> list[SecondFactorEnrollment]:
    """Return second-factor enrollments for a user."""
    user_uuid = UUID(str(user_id))
    query = select(SecondFactorEnrollment).where(SecondFactorEnrollment.user_id == user_uuid)
    if active_only:
        query = query.where(SecondFactorEnrollment.is_active.is_(True))

    result = await db_session.execute(
        query.order_by(SecondFactorEnrollment.created_at.desc())
    )
    return list(result.scalars().all())


async def list_second_factor_enrollments_for_factor(
    db_session,
    user_id: str,
    factor_key: str,
    *,
    active_only: bool = True,
) -> list[SecondFactorEnrollment]:
    """Return second-factor enrollments for a user scoped to a specific factor key."""
    user_uuid = UUID(str(user_id))
    query = select(SecondFactorEnrollment).where(
        SecondFactorEnrollment.user_id == user_uuid,
        SecondFactorEnrollment.factor_key == factor_key,
    )
    if active_only:
        query = query.where(SecondFactorEnrollment.is_active.is_(True))

    result = await db_session.execute(
        query.order_by(SecondFactorEnrollment.created_at.desc())
    )
    return list(result.scalars().all())


async def get_second_factor_enrollment_by_credential_id(
    db_session,
    *,
    factor_key: str,
    credential_id: str,
    active_only: bool = True,
) -> SecondFactorEnrollment | None:
    """Look up an enrolled second-factor credential by factor key and credential ID."""
    query = select(SecondFactorEnrollment).where(
        SecondFactorEnrollment.factor_key == factor_key,
        SecondFactorEnrollment.credential_id == credential_id,
    )
    if active_only:
        query = query.where(SecondFactorEnrollment.is_active.is_(True))

    result = await db_session.execute(query.limit(1))
    return result.scalar_one_or_none()


async def save_passkey_enrollment(
    db_session,
    *,
    user_id: str,
    factor_key: str,
    display_name: str | None,
    credential_id: str,
    public_key: str,
    sign_count: int,
    transports: list[str],
    enrollment_metadata: dict | None = None,
) -> SecondFactorEnrollment:
    """Create or update a passkey enrollment record."""
    enrollment = await get_second_factor_enrollment_by_credential_id(
        db_session,
        factor_key=factor_key,
        credential_id=credential_id,
        active_only=False,
    )
    now = datetime.now(UTC)
    if enrollment is None:
        enrollment = SecondFactorEnrollment(
            user_id=UUID(str(user_id)),
            factor_key=factor_key,
            factor_type="passkey",
            display_name=display_name,
            credential_id=credential_id,
            public_key=public_key,
            sign_count=sign_count,
            transports="\n".join(transports) if transports else None,
            enrollment_metadata=dict(enrollment_metadata or {}),
            enrolled_at=now,
            is_active=True,
        )
        db_session.add(enrollment)
        await db_session.flush()
        return enrollment

    if str(enrollment.user_id) != str(user_id):
        raise ValueError("Credential is already enrolled for a different user")

    enrollment.user_id = UUID(str(user_id))
    enrollment.factor_type = "passkey"
    enrollment.display_name = display_name
    enrollment.public_key = public_key
    enrollment.sign_count = sign_count
    enrollment.transports = "\n".join(transports) if transports else None
    enrollment.enrollment_metadata = dict(enrollment_metadata or {})
    enrollment.enrolled_at = enrollment.enrolled_at or now
    enrollment.is_active = True
    return enrollment


def touch_second_factor_enrollment(
    enrollment: SecondFactorEnrollment,
    *,
    sign_count: int | None = None,
    verification_metadata: dict | None = None,
) -> None:
    """Update usage metadata for a second-factor enrollment."""
    enrollment.last_used_at = datetime.now(UTC)
    if sign_count is not None:
        enrollment.sign_count = sign_count
    if verification_metadata:
        metadata = dict(enrollment.enrollment_metadata or {})
        metadata.update(verification_metadata)
        enrollment.enrollment_metadata = metadata


async def list_available_second_factor_descriptors(
    db_session,
    settings,
    user_id: str,
) -> list[SecondFactorMethodDescriptor]:
    """Return verification-capable second-factor methods enrolled for a user."""
    enrollments = await list_second_factor_enrollments(db_session, user_id, active_only=True)
    descriptors: list[SecondFactorMethodDescriptor] = []
    seen: set[str] = set()

    for enrollment in enrollments:
        if enrollment.factor_key in seen:
            continue
        seen.add(enrollment.factor_key)
        descriptor = get_second_factor_method(enrollment.factor_key).get_descriptor(settings)
        if descriptor.is_available:
            descriptors.append(descriptor)

    return descriptors


async def build_second_factor_transition_decision(
    db_session,
    settings,
    login_result,
    pending_auth: PendingAuthState,
) -> PendingAuthTransitionDecision:
    """Build the default pending-auth transition decision from second-factor config."""
    if getattr(login_result, "method_type", "") == "passkey":
        return PendingAuthTransitionDecision(promote_immediately=True)

    sf_settings = settings.auth.second_factors
    if not sf_settings.enabled or not sf_settings.challenge_on_enrolled:
        return PendingAuthTransitionDecision(promote_immediately=True)

    if pending_auth.user_id is None:
        return PendingAuthTransitionDecision(promote_immediately=True)

    available_methods = await list_available_second_factor_descriptors(
        db_session,
        settings,
        pending_auth.user_id,
    )
    if not available_methods:
        return PendingAuthTransitionDecision(promote_immediately=True)

    return PendingAuthTransitionDecision(
        promote_immediately=False,
        next_url="/auth/verify",
        stage=PENDING_AUTH_STAGE_SECOND_FACTOR_REQUIRED,
    )
