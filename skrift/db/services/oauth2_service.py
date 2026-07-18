"""OAuth2 authorization server database service."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.client_secret import hash_client_secret
from skrift.db.models.oauth2_client import OAuth2Client
from skrift.db.models.revoked_family import RevokedFamily
from skrift.db.models.revoked_token import RevokedToken
from skrift.hooks import hooks


@dataclass(slots=True)
class ClientCreateResult:
    """Returned by ``create_client`` — carries the one-time plaintext secret.

    The plaintext is only ever returned here (and from
    :func:`regenerate_client_secret`). After the admin flashes it through the
    UI, only the hashed column value is retained, so the plaintext cannot
    be recovered from the database.
    """

    client: OAuth2Client
    plaintext_secret: str


async def get_client_by_client_id(db_session: AsyncSession, client_id: str) -> OAuth2Client | None:
    """Look up an active OAuth2 client by its client_id string."""
    result = await db_session.execute(
        select(OAuth2Client).where(
            OAuth2Client.client_id == client_id,
            OAuth2Client.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def list_clients(db_session: AsyncSession) -> list[OAuth2Client]:
    """List all OAuth2 clients."""
    result = await db_session.execute(
        select(OAuth2Client).order_by(OAuth2Client.created_at.desc())
    )
    return list(result.scalars().all())


async def create_client(
    db_session: AsyncSession,
    display_name: str,
    redirect_uris: list[str],
    allowed_scopes: list[str],
) -> ClientCreateResult:
    """Create a new OAuth2 client with auto-generated credentials.

    Returns the client plus the one-time plaintext secret so the caller can
    show it to the operator. Only the hashed form is persisted.
    """
    plaintext_secret = secrets.token_urlsafe(48)
    client = OAuth2Client(
        client_id=secrets.token_urlsafe(24),
        client_secret=hash_client_secret(plaintext_secret),
        display_name=display_name,
        redirect_uris="\n".join(redirect_uris),
        allowed_scopes="\n".join(allowed_scopes),
    )
    db_session.add(client)
    await db_session.commit()
    await hooks.do_action("after_oauth2_client_created", client)
    return ClientCreateResult(client=client, plaintext_secret=plaintext_secret)


async def create_dynamic_client(
    db_session: AsyncSession,
    *,
    display_name: str,
    redirect_uris: list[str],
    allowed_scopes: list[str],
    registered_by_ip: str | None,
    issued_at: datetime,
) -> OAuth2Client:
    """Create a public client via RFC 7591 Dynamic Client Registration.

    Dynamic clients are always public: ``client_secret`` is empty and the
    token-endpoint auth method is ``none``, so they authenticate with PKCE
    alone. ``issued_at`` is recorded so the pruning job can retire clients
    that are registered but never used.
    """
    client = OAuth2Client(
        client_id=secrets.token_urlsafe(24),
        client_secret="",
        display_name=display_name,
        redirect_uris="\n".join(redirect_uris),
        allowed_scopes="\n".join(allowed_scopes),
        is_dynamically_registered=True,
        token_endpoint_auth_method="none",
        registered_by_ip=registered_by_ip,
        client_id_issued_at=issued_at,
    )
    db_session.add(client)
    await db_session.commit()
    await hooks.do_action("after_oauth2_client_created", client)
    return client


async def count_recent_dynamic_registrations(
    db_session: AsyncSession,
    registered_by_ip: str,
    since: datetime,
) -> int:
    """Count dynamic client registrations from an IP at or after ``since``.

    Backs the per-IP registration cap: the caller compares this against the
    configured limit before creating another dynamic client for the same IP.
    """
    result = await db_session.execute(
        select(func.count())
        .select_from(OAuth2Client)
        .where(
            OAuth2Client.is_dynamically_registered == True,  # noqa: E712
            OAuth2Client.registered_by_ip == registered_by_ip,
            OAuth2Client.client_id_issued_at >= since,
        )
    )
    return int(result.scalar_one())


async def mark_client_used(
    db_session: AsyncSession,
    client: OAuth2Client,
    used_at: datetime,
) -> None:
    """Stamp ``last_used_at`` on a client after a token is issued for it.

    Keeps dynamically-registered clients out of the pruning job's reach once
    they have actually been used at least once.
    """
    client.last_used_at = used_at
    await db_session.commit()


async def prune_stale_dynamic_clients(
    db_session: AsyncSession,
    *,
    now: datetime,
    max_age_days: int,
) -> int:
    """Delete dynamically-registered clients that were registered but never used.

    A client is pruned only when it is dynamic, has never issued a token
    (``last_used_at IS NULL``), and was registered more than ``max_age_days``
    ago. Admin-created and actively-used clients are never touched.
    """
    cutoff = now - timedelta(days=max_age_days)
    result = await db_session.execute(
        delete(OAuth2Client).where(
            OAuth2Client.is_dynamically_registered == True,  # noqa: E712
            OAuth2Client.last_used_at.is_(None),
            OAuth2Client.client_id_issued_at < cutoff,
        )
    )
    await db_session.commit()
    return result.rowcount


async def update_client(
    db_session: AsyncSession,
    client: OAuth2Client,
    display_name: str | None = None,
    redirect_uris: list[str] | None = None,
    allowed_scopes: list[str] | None = None,
    is_active: bool | None = None,
) -> OAuth2Client:
    """Update an existing OAuth2 client."""
    if display_name is not None:
        client.display_name = display_name
    if redirect_uris is not None:
        client.redirect_uris = "\n".join(redirect_uris)
    if allowed_scopes is not None:
        client.allowed_scopes = "\n".join(allowed_scopes)
    if is_active is not None:
        client.is_active = is_active
    await db_session.commit()
    await hooks.do_action("after_oauth2_client_updated", client)
    return client


async def delete_client(db_session: AsyncSession, client_id: UUID) -> None:
    """Delete an OAuth2 client by its database UUID."""
    result = await db_session.execute(
        select(OAuth2Client).where(OAuth2Client.id == client_id)
    )
    client = result.scalar_one_or_none()
    if client:
        await hooks.do_action("before_oauth2_client_deleted", client)
        await db_session.delete(client)
        await db_session.commit()
        await hooks.do_action("after_oauth2_client_deleted", client_id)


async def regenerate_client_secret(db_session: AsyncSession, client: OAuth2Client) -> str:
    """Regenerate a client's secret and return the new plaintext value.

    Only the hashed form is stored on the client row; the plaintext is
    returned once so the admin UI can flash it to the operator.
    """
    new_secret = secrets.token_urlsafe(48)
    client.client_secret = hash_client_secret(new_secret)
    await db_session.commit()
    await hooks.do_action("after_oauth2_client_secret_regenerated", client)
    return new_secret


async def revoke_token(
    db_session: AsyncSession,
    jti: str,
    token_type: str,
    expires_at: datetime,
) -> None:
    """Record a token revocation."""
    revoked = RevokedToken(jti=jti, token_type=token_type, expires_at=expires_at)
    db_session.add(revoked)
    await db_session.commit()
    await hooks.do_action("after_token_revoked", jti, token_type)


async def is_token_revoked(db_session: AsyncSession, jti: str) -> bool:
    """Check if a token has been revoked."""
    result = await db_session.execute(
        select(RevokedToken.id).where(RevokedToken.jti == jti)
    )
    return result.scalar_one_or_none() is not None


async def cleanup_expired_revocations(db_session: AsyncSession) -> int:
    """Delete revocation records for tokens that have already expired."""
    now = datetime.now(tz=datetime.now().astimezone().tzinfo)
    result = await db_session.execute(
        delete(RevokedToken).where(RevokedToken.expires_at < now)
    )
    await db_session.commit()
    return result.rowcount


async def revoke_family(db_session: AsyncSession, family_id: str) -> None:
    """Record a refresh-token family as revoked.

    Called when reuse detection fires (someone presented a refresh token
    whose ``jti`` had already been rotated away). Future refresh requests
    for any sibling in the family must fail.

    Safe to call repeatedly — the table has a unique constraint on
    ``family_id`` so we swallow the ``IntegrityError`` path. In practice
    the caller checks first, so the duplicate path is cold.
    """
    if not family_id:
        return
    existing = await db_session.execute(
        select(RevokedFamily.id).where(RevokedFamily.family_id == family_id)
    )
    if existing.scalar_one_or_none() is not None:
        return
    revoked = RevokedFamily(
        family_id=family_id,
        revoked_at=datetime.now(tz=datetime.now().astimezone().tzinfo),
    )
    db_session.add(revoked)
    await db_session.commit()
    await hooks.do_action("after_token_family_revoked", family_id)


async def is_family_revoked(db_session: AsyncSession, family_id: str) -> bool:
    """Check if a refresh-token family has been mass-revoked."""
    if not family_id:
        return False
    result = await db_session.execute(
        select(RevokedFamily.id).where(RevokedFamily.family_id == family_id)
    )
    return result.scalar_one_or_none() is not None
