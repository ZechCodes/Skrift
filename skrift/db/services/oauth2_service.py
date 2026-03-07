"""OAuth2 authorization server database service."""

import secrets
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.oauth2_client import OAuth2Client
from skrift.db.models.revoked_token import RevokedToken
from skrift.lib.hooks import hooks


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
) -> OAuth2Client:
    """Create a new OAuth2 client with auto-generated credentials."""
    client = OAuth2Client(
        client_id=secrets.token_urlsafe(24),
        client_secret=secrets.token_urlsafe(48),
        display_name=display_name,
        redirect_uris="\n".join(redirect_uris),
        allowed_scopes="\n".join(allowed_scopes),
    )
    db_session.add(client)
    await db_session.commit()
    await hooks.do_action("after_oauth2_client_created", client)
    return client


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
    """Regenerate a client's secret and return the new value."""
    new_secret = secrets.token_urlsafe(48)
    client.client_secret = new_secret
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
