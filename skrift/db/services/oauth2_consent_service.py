"""OAuth2 remembered-consent service.

Persists a user's approval of an OAuth2 client's scopes so the
``/oauth/authorize`` consent screen can be skipped when a later request stays
within the already-granted scopes, and cascades client deletion to the grants
that referenced the client.
"""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.oauth2_consent_grant import ConsentGrant
from skrift.hooks import hooks


async def find_grant(
    db_session: AsyncSession, user_id: str, client_id: str
) -> ConsentGrant | None:
    """Return the remembered consent grant for a user + client, if any."""
    result = await db_session.execute(
        select(ConsentGrant).where(
            ConsentGrant.user_id == user_id,
            ConsentGrant.client_id == client_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_grant(
    db_session: AsyncSession,
    *,
    user_id: str,
    client_id: str,
    scopes: list[str],
    granted_at: datetime,
    declined_scopes: list[str] | None = None,
) -> ConsentGrant:
    """Create or update a user's remembered consent for a client.

    Newly approved scopes are unioned with any previously granted scopes so a
    remembered grant only ever grows — a narrower later request never silently
    drops a scope the user already consented to.

    ``declined_scopes`` is the exception: scopes the user was shown and
    actively unchecked are removed from the grant. Without that, the union
    would restore a scope the user just refused and the next authorization
    request would skip the consent screen and hand it back.
    """
    grant = await find_grant(db_session, user_id, client_id)
    existing_scopes = set(grant.scope_list) if grant is not None else set()
    merged_scopes = "\n".join(
        sorted((existing_scopes | set(scopes)) - set(declined_scopes or []))
    )
    if grant is None:
        grant = ConsentGrant(
            user_id=user_id,
            client_id=client_id,
            scopes=merged_scopes,
            granted_at=granted_at,
        )
        db_session.add(grant)
    else:
        grant.scopes = merged_scopes
        grant.granted_at = granted_at
    await db_session.commit()
    await hooks.do_action("after_oauth2_consent_granted", grant)
    return grant


async def revoke_grant(db_session: AsyncSession, user_id: str, client_id: str) -> None:
    """Delete a user's remembered consent for a client."""
    await db_session.execute(
        delete(ConsentGrant).where(
            ConsentGrant.user_id == user_id,
            ConsentGrant.client_id == client_id,
        )
    )
    await db_session.commit()
    await hooks.do_action("after_oauth2_consent_revoked", user_id, client_id)


async def delete_grants_for_clients(
    db_session: AsyncSession, client_ids: list[str]
) -> int:
    """Delete every remembered consent grant for the given client_ids.

    Cascades an OAuth2 client deletion — the admin delete action and the
    dynamic-client pruning sweep — to the consent grants that referenced it.
    """
    if not client_ids:
        return 0
    result = await db_session.execute(
        delete(ConsentGrant).where(ConsentGrant.client_id.in_(client_ids))
    )
    await db_session.commit()
    return result.rowcount
