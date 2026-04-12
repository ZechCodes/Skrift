"""Shared find-or-create service for OAuth account linking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
) -> LoginResult:
    """Find or create a user for a generic primary-auth identity.

    The current persistence backend remains ``oauth_accounts`` for all built-in
    methods. This creates a method-agnostic service boundary without changing
    existing storage yet.
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
        oauth_account = OAuthAccount(
            provider=identity.method_key,
            provider_account_id=identity.subject_id,
            provider_email=identity.email,
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


async def find_or_create_oauth_user(
    db_session: AsyncSession,
    provider: str,
    user_data: NormalizedUserData,
    raw_user_info: dict,
    *,
    tokens: dict | None = None,
) -> LoginResult:
    """Find an existing user by OAuth account or email, or create a new one.

    Three-step lookup:
    1. Check for existing OAuth account (provider + provider_account_id)
    2. If email provided, check for existing user by email
    3. Create new user + OAuth account
    """
    identity = identity_from_oauth_user_data(
        method_key=provider,
        method_type="oauth",
        user_data=user_data,
        raw_metadata=raw_user_info,
    )
    return await find_or_create_user_for_identity(db_session, identity, tokens=tokens)
