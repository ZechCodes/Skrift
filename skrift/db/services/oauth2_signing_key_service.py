"""OAuth2 signing key service — asymmetric access-token key lifecycle.

Access tokens are ES256 JWTs signed by a rotating EC P-256 key. The active
key signs new tokens; retired keys stay published at the JWKS endpoint until
they age past the access-token lifetime so tokens they signed keep verifying.
"""

from datetime import datetime, timedelta, timezone

from joserfc.jwk import ECKey
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.oauth2_signing_key import OAuth2SigningKey
from skrift.hooks import hooks


SIGNING_ALGORITHM = "ES256"
_ELLIPTIC_CURVE = "P-256"


def _generate_key_material() -> tuple[str, str]:
    """Generate a fresh EC P-256 key pair.

    Returns the RFC 7638 thumbprint (used as the ``kid``) and the private
    key PEM. Pure — performs no database work.
    """
    key = ECKey.generate_key(_ELLIPTIC_CURVE)
    kid = key.thumbprint()
    private_key_pem = key.as_pem(private=True).decode("ascii")
    return kid, private_key_pem


def public_jwk(signing_key: OAuth2SigningKey) -> dict:
    """Render a signing key's public half as a JWK dict for the JWKS set.

    Pure — reads only the passed model. The ``kid``, ``use`` and ``alg``
    are attached so third-party verifiers can select the matching key.
    """
    key = ECKey.import_key(
        signing_key.private_key_pem,
        parameters={"kid": signing_key.kid, "use": "sig", "alg": signing_key.algorithm},
    )
    return key.as_dict(private=False)


async def get_or_create_active_key(db_session: AsyncSession) -> OAuth2SigningKey:
    """Return the active signing key, generating one on first use."""
    result = await db_session.execute(
        select(OAuth2SigningKey).where(OAuth2SigningKey.is_active == True)  # noqa: E712
    )
    active_key = result.scalars().first()
    if active_key is not None:
        return active_key

    kid, private_key_pem = _generate_key_material()
    active_key = OAuth2SigningKey(
        kid=kid,
        private_key_pem=private_key_pem,
        algorithm=SIGNING_ALGORITHM,
        is_active=True,
    )
    db_session.add(active_key)
    await db_session.commit()
    await hooks.do_action("after_oauth2_signing_key_created", active_key)
    return active_key


async def rotate(db_session: AsyncSession) -> OAuth2SigningKey:
    """Promote a freshly generated key to active and retire the old one.

    Retired keys keep their rows (and stay published in the JWKS set) until
    :func:`prune_expired` removes them once they have aged out.
    """
    now = datetime.now(tz=timezone.utc)
    result = await db_session.execute(
        select(OAuth2SigningKey).where(OAuth2SigningKey.is_active == True)  # noqa: E712
    )
    for previous_key in result.scalars().all():
        previous_key.is_active = False
        previous_key.retired_at = now

    kid, private_key_pem = _generate_key_material()
    new_key = OAuth2SigningKey(
        kid=kid,
        private_key_pem=private_key_pem,
        algorithm=SIGNING_ALGORITHM,
        is_active=True,
    )
    db_session.add(new_key)
    await db_session.commit()
    await hooks.do_action("after_oauth2_signing_key_rotated", new_key)
    return new_key


async def list_jwks_keys(db_session: AsyncSession) -> list[dict]:
    """Return public JWK dicts for every key still published in the JWKS set.

    Keys that :func:`prune_expired` has already removed are gone from the
    table, so every remaining row is fair to publish.
    """
    result = await db_session.execute(
        select(OAuth2SigningKey).order_by(
            OAuth2SigningKey.is_active.desc(), OAuth2SigningKey.created_at.desc()
        )
    )
    return [public_jwk(signing_key) for signing_key in result.scalars().all()]


async def prune_expired(
    db_session: AsyncSession,
    now: datetime,
    access_token_ttl: int,
    clock_skew: int,
) -> int:
    """Delete retired keys older than the access-token lifetime plus skew.

    A key is safe to drop once ``retired_at + access_token_ttl + clock_skew``
    has passed, since no unexpired token could still be signed by it. Returns
    the number of rows deleted.
    """
    cutoff = now - timedelta(seconds=access_token_ttl + clock_skew)
    result = await db_session.execute(
        delete(OAuth2SigningKey).where(
            OAuth2SigningKey.retired_at.is_not(None),
            OAuth2SigningKey.retired_at < cutoff,
        )
    )
    await db_session.commit()
    return result.rowcount
