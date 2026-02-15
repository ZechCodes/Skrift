"""Shared find-or-create service for OAuth account linking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.providers import NormalizedUserData
from skrift.db.models.oauth_account import OAuthAccount
from skrift.db.models.user import User


@dataclass
class LoginResult:
    """Result of an OAuth login attempt."""

    user: User
    oauth_account: OAuthAccount
    is_new_user: bool


async def find_or_create_oauth_user(
    db_session: AsyncSession,
    provider: str,
    user_data: NormalizedUserData,
    raw_user_info: dict,
) -> LoginResult:
    """Find an existing user by OAuth account or email, or create a new one.

    Three-step lookup:
    1. Check for existing OAuth account (provider + provider_account_id)
    2. If email provided, check for existing user by email
    3. Create new user + OAuth account
    """
    # Step 1: Check if OAuth account already exists
    result = await db_session.execute(
        select(OAuthAccount)
        .options(selectinload(OAuthAccount.user))
        .where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_account_id == user_data.oauth_id,
        )
    )
    oauth_account = result.scalar_one_or_none()

    if oauth_account:
        user = oauth_account.user
        user.name = user_data.name
        if user_data.picture_url:
            user.picture_url = user_data.picture_url
        user.last_login_at = datetime.now(UTC)
        if user_data.email:
            oauth_account.provider_email = user_data.email
        oauth_account.provider_metadata = raw_user_info
        return LoginResult(user=user, oauth_account=oauth_account, is_new_user=False)

    # Step 2: Check if a user with this email already exists
    user = None
    if user_data.email:
        result = await db_session.execute(
            select(User).where(User.email == user_data.email)
        )
        user = result.scalar_one_or_none()

    if user:
        oauth_account = OAuthAccount(
            provider=provider,
            provider_account_id=user_data.oauth_id,
            provider_email=user_data.email,
            provider_metadata=raw_user_info,
            user_id=user.id,
        )
        db_session.add(oauth_account)
        user.name = user_data.name
        if user_data.picture_url:
            user.picture_url = user_data.picture_url
        user.last_login_at = datetime.now(UTC)
        return LoginResult(user=user, oauth_account=oauth_account, is_new_user=False)

    # Step 3: Create new user + OAuth account
    user = User(
        email=user_data.email,
        name=user_data.name,
        picture_url=user_data.picture_url,
        last_login_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()

    oauth_account = OAuthAccount(
        provider=provider,
        provider_account_id=user_data.oauth_id,
        provider_email=user_data.email,
        provider_metadata=raw_user_info,
        user_id=user.id,
    )
    db_session.add(oauth_account)
    return LoginResult(user=user, oauth_account=oauth_account, is_new_user=True)
