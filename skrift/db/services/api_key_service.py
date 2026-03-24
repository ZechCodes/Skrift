"""API key database service."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.db.models.api_key import APIKey
from skrift.lib.hooks import hooks


def _generate_key() -> tuple[str, str, str]:
    """Generate an API key and return (raw_key, prefix, hash).

    Key format: ``sk_<token>`` where token is 32 bytes of URL-safe randomness.
    """
    raw = f"sk_{secrets.token_urlsafe(32)}"
    prefix = raw[:12]
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, key_hash


def _generate_refresh_token() -> tuple[str, str]:
    """Generate a refresh token and return (raw_token, hash).

    Refresh token format: ``skr_<token>``.
    """
    raw = f"skr_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


def _hash_token(raw: str) -> str:
    """SHA-256 hash a raw token string."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_api_key(
    db_session: AsyncSession,
    user_id: UUID | str,
    display_name: str,
    *,
    description: str | None = None,
    scoped_permissions: list[str] | None = None,
    scoped_roles: list[str] | None = None,
    expires_at: datetime | None = None,
    refresh_token_expiration_days: int = 30,
) -> tuple[APIKey, str, str]:
    """Create a new API key.

    Returns:
        Tuple of (api_key_model, raw_key, raw_refresh_token).
        The raw values are only available at creation time.
    """
    raw_key, prefix, key_hash = _generate_key()
    raw_refresh, refresh_hash = _generate_refresh_token()

    refresh_expires = datetime.now(tz=timezone.utc) + timedelta(days=refresh_token_expiration_days)

    api_key = APIKey(
        user_id=str(user_id),
        display_name=display_name,
        description=description,
        key_prefix=prefix,
        key_hash=key_hash,
        scoped_permissions="\n".join(scoped_permissions) if scoped_permissions else None,
        scoped_roles="\n".join(scoped_roles) if scoped_roles else None,
        expires_at=expires_at,
        refresh_token_hash=refresh_hash,
        refresh_token_expires_at=refresh_expires,
    )
    db_session.add(api_key)
    await db_session.commit()
    await hooks.do_action("after_api_key_created", api_key)
    return api_key, raw_key, raw_refresh


async def verify_api_key(
    db_session: AsyncSession,
    raw_key: str,
    *,
    client_ip: str | None = None,
) -> APIKey | None:
    """Verify an API key and return the model if valid.

    Checks: exists, is_active, not expired, user is_active.
    Updates last_used_at and last_used_ip on success.
    """
    key_hash = _hash_token(raw_key)
    result = await db_session.execute(
        select(APIKey)
        .options(selectinload(APIKey.user))
        .where(APIKey.key_hash == key_hash)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        return None

    # Validity checks
    if not api_key.is_active:
        return None
    if api_key.is_expired:
        return None
    if not api_key.user.is_active:
        return None

    # Update usage tracking
    api_key.last_used_at = datetime.now(tz=timezone.utc)
    if client_ip:
        api_key.last_used_ip = client_ip
    await db_session.commit()

    return api_key


async def refresh_api_key(
    db_session: AsyncSession,
    raw_refresh_token: str,
    *,
    refresh_token_expiration_days: int = 30,
) -> tuple[APIKey, str, str] | None:
    """Rotate an API key using its refresh token.

    Atomically generates a new key and refresh token. The old key
    and refresh token stop working immediately.

    Returns:
        Tuple of (api_key_model, new_raw_key, new_raw_refresh_token),
        or None if the refresh token is invalid/expired.
    """
    token_hash = _hash_token(raw_refresh_token)
    result = await db_session.execute(
        select(APIKey)
        .options(selectinload(APIKey.user))
        .where(APIKey.refresh_token_hash == token_hash)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        return None

    if not api_key.is_active:
        return None
    if api_key.refresh_token_expired:
        return None
    if not api_key.user.is_active:
        return None

    # Generate new credentials
    new_raw_key, new_prefix, new_key_hash = _generate_key()
    new_raw_refresh, new_refresh_hash = _generate_refresh_token()

    api_key.key_prefix = new_prefix
    api_key.key_hash = new_key_hash
    api_key.refresh_token_hash = new_refresh_hash
    api_key.refresh_token_expires_at = datetime.now(tz=timezone.utc) + timedelta(
        days=refresh_token_expiration_days
    )

    await db_session.commit()
    await hooks.do_action("after_api_key_refreshed", api_key)
    return api_key, new_raw_key, new_raw_refresh


async def list_api_keys(
    db_session: AsyncSession,
    *,
    user_id: UUID | str | None = None,
) -> list[APIKey]:
    """List API keys, optionally filtered by user."""
    query = select(APIKey).options(selectinload(APIKey.user)).order_by(APIKey.created_at.desc())
    if user_id is not None:
        query = query.where(APIKey.user_id == str(user_id))
    result = await db_session.execute(query)
    return list(result.scalars().all())


async def get_api_key(db_session: AsyncSession, key_id: UUID | str) -> APIKey | None:
    """Get a single API key by its database ID."""
    result = await db_session.execute(
        select(APIKey)
        .options(selectinload(APIKey.user))
        .where(APIKey.id == str(key_id))
    )
    return result.scalar_one_or_none()


async def update_api_key(
    db_session: AsyncSession,
    api_key: APIKey,
    *,
    display_name: str | None = None,
    description: str | None = ...,
    scoped_permissions: list[str] | None = ...,
    scoped_roles: list[str] | None = ...,
    expires_at: datetime | None = ...,
    is_active: bool | None = None,
) -> APIKey:
    """Update an API key's metadata."""
    if display_name is not None:
        api_key.display_name = display_name
    if description is not ...:
        api_key.description = description
    if scoped_permissions is not ...:
        api_key.scoped_permissions = "\n".join(scoped_permissions) if scoped_permissions else None
    if scoped_roles is not ...:
        api_key.scoped_roles = "\n".join(scoped_roles) if scoped_roles else None
    if expires_at is not ...:
        api_key.expires_at = expires_at
    if is_active is not None:
        api_key.is_active = is_active
    await db_session.commit()
    await hooks.do_action("after_api_key_updated", api_key)
    return api_key


async def revoke_api_key(db_session: AsyncSession, key_id: UUID | str) -> None:
    """Revoke an API key (set is_active=False)."""
    result = await db_session.execute(
        select(APIKey).where(APIKey.id == str(key_id))
    )
    api_key = result.scalar_one_or_none()
    if api_key:
        api_key.is_active = False
        await db_session.commit()
        await hooks.do_action("after_api_key_revoked", api_key)


async def delete_api_key(db_session: AsyncSession, key_id: UUID | str) -> None:
    """Permanently delete an API key."""
    result = await db_session.execute(
        select(APIKey).where(APIKey.id == str(key_id))
    )
    api_key = result.scalar_one_or_none()
    if api_key:
        await hooks.do_action("before_api_key_deleted", api_key)
        await db_session.delete(api_key)
        await db_session.commit()
        await hooks.do_action("after_api_key_deleted", key_id)
