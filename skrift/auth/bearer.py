"""Bearer JWT route guards for OAuth2-protected resources.

``BearerJWTAuth``/``BearerJWTOnly`` are marker guards (the same pattern as
``APIKeyAuth``/``APIKeyOnly`` in :mod:`skrift.auth.guards`): listing one on a
route switches ``auth_guard`` into accepting ES256 access-token JWTs minted
by the OAuth2 Authorization Server. The two bearer schemes share the
``Authorization`` header, so routing is disambiguated by shape — API keys are
``sk_``-prefixed, a compact JWS has exactly three dot-separated segments.

Verification is fail-closed: signature (against the signing-key JWKS),
``iss``, ``exp``, audience membership in ``aud``, ``jti`` revocation, required
scopes, and an existing active subject are all enforced before the request is
granted the user's permissions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException

from skrift.auth.guards import AuthRequirement
from skrift.auth.jwt_tokens import verify_access_token_jwt
from skrift.auth.services import get_user_permissions
from skrift.config import get_settings
from skrift.db.models.user import User
from skrift.db.services import oauth2_service, oauth2_signing_key_service

if TYPE_CHECKING:
    from skrift.auth.services import UserPermissions


class BearerJWTAuth(AuthRequirement):
    """Marker guard: route accepts session auth and OAuth2 JWT bearer tokens.

    ``audience`` is the canonical resource identifier this route serves
    (e.g. ``https://app.example.com/mcp``); only tokens carrying it in
    ``aud`` are accepted. ``scopes`` lists OAuth2 scopes the token must have
    been granted.

    Usage::

        @get("/mcp/pages", guards=[auth_guard, BearerJWTAuth(audience="https://app.example.com/mcp", scopes=["mcp"]), Permission("manage-pages")])
        async def list_pages(self): ...
    """

    def __init__(self, audience: str, scopes: list[str] | None = None):
        self.audience = audience
        self.required_scopes = list(scopes) if scopes else []

    async def check(self, permissions: "UserPermissions") -> bool:
        return True  # Marker only — actual verification happens in auth_guard


class BearerJWTOnly(BearerJWTAuth):
    """Marker guard: route accepts ONLY JWT bearer authentication (no session).

    Usage::

        @post("/mcp/sync", guards=[auth_guard, BearerJWTOnly(audience="https://app.example.com/mcp")])
        async def sync(self): ...
    """


def is_jwt_shaped(token: str) -> bool:
    """Whether a bearer token is a compact JWS rather than an API key.

    API keys are ``sk_``-prefixed and take precedence; a compact JWS is
    exactly three dot-separated segments. This is the single routing rule
    keeping the two bearer schemes from colliding.
    """
    return not token.startswith("sk_") and token.count(".") == 2


def _invalid_token_error(description: str) -> NotAuthorizedException:
    return NotAuthorizedException(
        description,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _insufficient_scope_error(required_scopes: list[str]) -> NotAuthorizedException:
    scope_value = " ".join(required_scopes)
    return NotAuthorizedException(
        "Insufficient scope",
        headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope_value}"'},
    )


def missing_bearer_error() -> NotAuthorizedException:
    """The RFC 6750 challenge for a protected route with no usable bearer."""
    return NotAuthorizedException(
        "Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def resolve_bearer_jwt_permissions(
    connection: ASGIConnection,
    token: str,
    marker: BearerJWTAuth,
) -> tuple[str, "UserPermissions"]:
    """Verify a JWT bearer token and resolve its subject's permissions.

    Returns ``(user_id, permissions)`` so JWT-authenticated requests carry
    the same :class:`UserPermissions` identity as the session and API-key
    paths, letting ``Permission()``/``Role()`` guards compose unchanged.
    Raises :class:`NotAuthorizedException` (fail closed) on any failure.
    """
    settings = get_settings()
    issuer = settings.oauth2_issuer or str(connection.base_url).rstrip("/")

    session_maker = connection.app.state.session_maker_class
    async with session_maker() as db_session:
        jwks = await oauth2_signing_key_service.list_jwks_keys(db_session)
        if not jwks:
            raise _invalid_token_error("No signing keys published")

        claims = verify_access_token_jwt(
            token, jwks=jwks, issuer=issuer, audience=marker.audience
        )
        if claims is None:
            raise _invalid_token_error("Invalid or expired access token")

        jti = claims.get("jti", "")
        if jti and await oauth2_service.is_token_revoked(db_session, jti):
            raise _invalid_token_error("Access token has been revoked")

        granted_scopes = set(claims.get("scope", "").split())
        if any(scope not in granted_scopes for scope in marker.required_scopes):
            raise _insufficient_scope_error(marker.required_scopes)

        subject = claims.get("sub", "")
        try:
            subject_uuid = UUID(subject)
        except (ValueError, TypeError):
            raise _invalid_token_error("Token subject is not a known user") from None

        user = await db_session.get(User, subject_uuid)
        if user is None or not user.is_active:
            raise _invalid_token_error("Token subject is not a known user")

        permissions = await get_user_permissions(db_session, subject)
        return subject, permissions
