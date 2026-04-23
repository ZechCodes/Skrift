"""Shared find-or-create service for OAuth account linking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.identities import ResolvedPrimaryIdentity, identity_from_oauth_user_data
from skrift.auth.providers import NormalizedUserData
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.second_factor import SecondFactorEnrollment
from skrift.db.models.user import User
from skrift.lib.hooks import hooks


@dataclass
class LoginResult:
    """Result of a primary-auth login attempt."""

    user: User
    identity_record: OAuthAccount | None
    is_new_user: bool
    method_key: str
    method_type: str

    @property
    def oauth_account(self) -> OAuthAccount | None:
        """Compatibility alias for existing OAuth-oriented callers."""
        return self.identity_record


@dataclass(slots=True)
class EmailVerificationRequired:
    """Signals that the caller must run an email-verification challenge.

    Returned by :func:`find_or_create_user_for_identity` when a provider
    reports an email that matches an existing user but did not attest that
    the email was verified. Callers must NOT log the user in; instead, issue
    a one-time verification link to ``identity.email`` and only complete the
    OAuth-account link once the clicker proves control of the inbox.
    """

    existing_user_id: str
    identity: ResolvedPrimaryIdentity
    tokens: dict[str, Any] | None = None


IdentityResolution = Union[LoginResult, EmailVerificationRequired]


async def build_login_result_for_user(
    user: User,
    *,
    method_key: str,
    method_type: str,
    identity_record: OAuthAccount | None = None,
    is_new_user: bool = False,
) -> LoginResult:
    """Build a LoginResult for an already-resolved user and update login metadata."""
    user.last_login_at = datetime.now(UTC)
    await hooks.do_action("after_user_update", user)
    return LoginResult(
        user=user,
        identity_record=identity_record,
        is_new_user=is_new_user,
        method_key=method_key,
        method_type=method_type,
    )


async def create_login_result_for_new_user(
    db_session: AsyncSession,
    *,
    email: str,
    name: str | None,
    picture_url: str | None,
    method_key: str,
    method_type: str,
) -> LoginResult:
    """Create a new user record and return a LoginResult for it."""
    user = User(
        email=email,
        name=name,
        picture_url=picture_url,
        last_login_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await hooks.do_action("after_user_created_db", user)
    return LoginResult(
        user=user,
        identity_record=None,
        is_new_user=True,
        method_key=method_key,
        method_type=method_type,
    )


async def find_login_result_for_passkey_credential(
    db_session: AsyncSession,
    *,
    factor_key: str,
    method_key: str,
    credential_id: str,
) -> LoginResult | None:
    """Resolve a LoginResult from an enrolled passkey credential."""
    result = await db_session.execute(
        select(SecondFactorEnrollment)
        .options(selectinload(SecondFactorEnrollment.user))
        .where(
            SecondFactorEnrollment.factor_key == factor_key,
            SecondFactorEnrollment.credential_id == credential_id,
            SecondFactorEnrollment.is_active.is_(True),
        )
        .limit(1)
    )
    enrollment = result.scalar_one_or_none()
    if enrollment is None or enrollment.user is None:
        return None

    return await build_login_result_for_user(
        enrollment.user,
        method_key=method_key,
        method_type="passkey",
    )


async def find_or_create_user_for_identity(
    db_session: AsyncSession,
    identity: ResolvedPrimaryIdentity,
    *,
    tokens: dict[str, Any] | None = None,
) -> IdentityResolution:
    """Find or create a user for a generic primary-auth identity.

    Three resolution branches:

    1. **Subject match** — an :class:`OAuthAccount` already exists for this
       ``(provider, subject_id)`` pair. Safe to log in directly; the
       provider's subject ID proves it controls this identity.
    2. **Email match** — the provider returned an email already associated
       with an existing user, and the provider attested verification
       (``identity.email_verified == True``). Auto-link and log in.
    3. **Email match without verification** — same as (2) but the provider
       did NOT attest verification. Return :class:`EmailVerificationRequired`
       so the caller can run an email-challenge flow. **No** ``OAuthAccount``
       is inserted until the challenge completes — this is the fix for the
       account-takeover vector.
    4. **No match** — create a fresh :class:`User` + :class:`OAuthAccount`.
    """
    result = await db_session.execute(
        select(OAuthAccount)
        .options(selectinload(OAuthAccount.user))
        .where(
            OAuthAccount.provider == identity.method_key,
            OAuthAccount.provider_account_id == identity.subject_id,
        )
    )
    oauth_account = result.scalar_one_or_none()

    if oauth_account:
        user = oauth_account.user
        user.name = identity.name
        if identity.picture_url:
            user.picture_url = identity.picture_url
        user.last_login_at = datetime.now(UTC)
        if identity.email:
            oauth_account.provider_email = identity.email
        # Subject match is authoritative — refresh the stored attestation.
        oauth_account.provider_email_verified = bool(identity.email_verified)
        oauth_account.provider_metadata = identity.raw_metadata
        if tokens:
            oauth_account.access_token = tokens.get("access_token")
            oauth_account.refresh_token = tokens.get("refresh_token")
        await hooks.do_action("after_user_update", user)
        return LoginResult(
            user=user,
            identity_record=oauth_account,
            is_new_user=False,
            method_key=identity.method_key,
            method_type=identity.method_type,
        )

    user = None
    if identity.email:
        result = await db_session.execute(select(User).where(User.email == identity.email))
        user = result.scalar_one_or_none()

    if user:
        # Email-match branch. Only auto-link when the provider attested the
        # email is verified — otherwise an attacker with a non-verifying
        # provider account claiming the victim's email can hijack this user.
        if not identity.email_verified:
            return EmailVerificationRequired(
                existing_user_id=str(user.id),
                identity=identity,
                tokens=tokens,
            )

        oauth_account = OAuthAccount(
            provider=identity.method_key,
            provider_account_id=identity.subject_id,
            provider_email=identity.email,
            provider_email_verified=True,
            provider_metadata=identity.raw_metadata,
            access_token=tokens.get("access_token") if tokens else None,
            refresh_token=tokens.get("refresh_token") if tokens else None,
            user_id=user.id,
        )
        db_session.add(oauth_account)
        user.name = identity.name
        if identity.picture_url:
            user.picture_url = identity.picture_url
        user.last_login_at = datetime.now(UTC)
        await hooks.do_action("after_user_update", user)
        return LoginResult(
            user=user,
            identity_record=oauth_account,
            is_new_user=False,
            method_key=identity.method_key,
            method_type=identity.method_type,
        )

    # No existing user — safe to create a new account regardless of email
    # verification; the provider is proving control of a fresh identity.
    user = User(
        email=identity.email,
        name=identity.name,
        picture_url=identity.picture_url,
        last_login_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await hooks.do_action("after_user_created_db", user)

    oauth_account = OAuthAccount(
        provider=identity.method_key,
        provider_account_id=identity.subject_id,
        provider_email=identity.email,
        provider_email_verified=bool(identity.email_verified),
        provider_metadata=identity.raw_metadata,
        access_token=tokens.get("access_token") if tokens else None,
        refresh_token=tokens.get("refresh_token") if tokens else None,
        user_id=user.id,
    )
    db_session.add(oauth_account)
    return LoginResult(
        user=user,
        identity_record=oauth_account,
        is_new_user=True,
        method_key=identity.method_key,
        method_type=identity.method_type,
    )


async def complete_verified_email_link(
    db_session: AsyncSession,
    *,
    existing_user_id: str,
    identity: ResolvedPrimaryIdentity,
    tokens: dict[str, Any] | None = None,
) -> LoginResult:
    """Complete the deferred OAuth account link after an email challenge.

    Called from the email-link claim handler once the clicker has proven
    control of the inbox. Idempotent: if the link already exists (e.g. user
    clicked twice before revocation landed), refresh it rather than raising.
    """
    result = await db_session.execute(
        select(OAuthAccount)
        .options(selectinload(OAuthAccount.user))
        .where(
            OAuthAccount.provider == identity.method_key,
            OAuthAccount.provider_account_id == identity.subject_id,
        )
    )
    oauth_account = result.scalar_one_or_none()

    if oauth_account is None:
        user_result = await db_session.execute(
            select(User).where(User.id == existing_user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise ValueError("Target user for email-verified link not found")
        oauth_account = OAuthAccount(
            provider=identity.method_key,
            provider_account_id=identity.subject_id,
            provider_email=identity.email,
            provider_email_verified=True,
            provider_metadata=identity.raw_metadata,
            access_token=tokens.get("access_token") if tokens else None,
            refresh_token=tokens.get("refresh_token") if tokens else None,
            user_id=user.id,
        )
        db_session.add(oauth_account)
    else:
        user = oauth_account.user
        oauth_account.provider_email = identity.email
        oauth_account.provider_email_verified = True
        oauth_account.provider_metadata = identity.raw_metadata
        if tokens:
            oauth_account.access_token = tokens.get("access_token")
            oauth_account.refresh_token = tokens.get("refresh_token")

    if identity.name:
        user.name = identity.name
    if identity.picture_url:
        user.picture_url = identity.picture_url
    user.last_login_at = datetime.now(UTC)
    await hooks.do_action("after_user_update", user)

    return LoginResult(
        user=user,
        identity_record=oauth_account,
        is_new_user=False,
        method_key=identity.method_key,
        method_type=identity.method_type,
    )


async def find_or_create_oauth_user(
    db_session: AsyncSession,
    provider: str,
    user_data: NormalizedUserData,
    raw_user_info: dict,
    *,
    tokens: dict | None = None,
) -> IdentityResolution:
    """Find an existing user by OAuth account or email, or create a new one.

    Returns the same discriminated union as
    :func:`find_or_create_user_for_identity`. Callers must handle both
    :class:`LoginResult` (ready to log in) and
    :class:`EmailVerificationRequired` (must run an email challenge before
    completing the link).
    """
    identity = identity_from_oauth_user_data(
        method_key=provider,
        method_type="oauth",
        user_data=user_data,
        raw_metadata=raw_user_info,
    )
    return await find_or_create_user_for_identity(db_session, identity, tokens=tokens)
