"""JWT access tokens for the OAuth2 Authorization Server.

Access tokens are ES256 JWTs (RFC 9068 profile) signed by the rotating server
key so third-party resource servers can verify them against the published
JWKS. Authorization codes and refresh tokens stay HMAC — see
``skrift.auth.tokens``.
"""

import time
import uuid

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, KeySet

from skrift.db.models.oauth2_signing_key import OAuth2SigningKey

ACCESS_TOKEN_JWT_TYPE = "at+jwt"
SUPPORTED_ACCESS_TOKEN_ALGORITHMS = ["ES256"]


def create_access_token_jwt(
    *,
    subject: str,
    client_id: str,
    scope: str,
    audience: str | None,
    issuer: str,
    signing_key: OAuth2SigningKey,
    expires_in: int,
) -> str:
    """Mint a signed access-token JWT.

    ``aud`` is set only when the grant carried an RFC 8707 resource; ``jti``
    is kept so revocation checks against :class:`RevokedToken` still work.
    """
    issued_at = int(time.time())
    claims: dict = {
        "iss": issuer,
        "sub": subject,
        "client_id": client_id,
        "scope": scope,
        "iat": issued_at,
        "exp": issued_at + expires_in,
        "jti": uuid.uuid4().hex,
    }
    if audience:
        claims["aud"] = audience

    header = {
        "alg": signing_key.algorithm,
        "kid": signing_key.kid,
        "typ": ACCESS_TOKEN_JWT_TYPE,
    }
    private_key = ECKey.import_key(signing_key.private_key_pem)
    return jwt.encode(header, claims, private_key, algorithms=[signing_key.algorithm])


def verify_access_token_jwt(
    token: str,
    *,
    jwks: list[dict],
    issuer: str,
    audience: str | None,
) -> dict | None:
    """Verify an access-token JWT against a JWK set.

    Validates the signature (selecting the JWKS key matching the token's
    ``kid``), ``exp``, ``iss``, and — when ``audience`` is given — that it
    appears in the token's ``aud``. Passing ``audience=None`` skips only the
    audience check (used by userinfo/introspection, which serve any audience).

    Returns the claims dict, or ``None`` on any failure (fail closed).
    """
    try:
        key_set = KeySet.import_key_set({"keys": jwks})
        decoded = jwt.decode(token, key_set, algorithms=SUPPORTED_ACCESS_TOKEN_ALGORITHMS)
    except (JoseError, ValueError):
        return None

    claims = decoded.claims
    if claims.get("iss") != issuer:
        return None

    expires_at = claims.get("exp")
    if not isinstance(expires_at, (int, float)) or time.time() > expires_at:
        return None

    if audience is not None and audience not in _audience_values(claims.get("aud")):
        return None

    return claims


def _audience_values(audience_claim) -> list[str]:
    """Normalize an ``aud`` claim (string, list, or absent) to a list."""
    if isinstance(audience_claim, str):
        return [audience_claim]
    if isinstance(audience_claim, list):
        return [value for value in audience_claim if isinstance(value, str)]
    return []
