"""Phase 3 MCP identity provider — reusable Bearer JWT route guard.

``BearerJWTAuth``/``BearerJWTOnly`` markers switch ``auth_guard`` into
accepting ES256 access-token JWTs minted by the OAuth2 Authorization Server.
The guard must fail closed: bad signature, wrong audience, expiry, revoked
``jti``, missing scope, or an unknown/inactive subject all yield 401 with an
RFC 6750 ``WWW-Authenticate`` challenge. API keys (``sk_`` bearers) and JWTs
share the ``Authorization`` header, so the two schemes must never collide.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from joserfc.jwk import ECKey
from litestar.exceptions import NotAuthorizedException

from skrift.auth.bearer import BearerJWTAuth, BearerJWTOnly, is_jwt_shaped
from skrift.auth.guards import APIKeyAuth, APIKeyOnly, Permission, auth_guard
from skrift.auth.jwt_tokens import create_access_token_jwt
from skrift.auth.services import UserPermissions
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.services import oauth2_signing_key_service as signing_keys


ISSUER = "http://localhost:8000"
RESOURCE = "https://app.example.com/mcp"
OTHER_RESOURCE = "https://other.example.com/api"
USER_ID = "00000000-0000-0000-0000-000000000042"
ACCESS_TTL = 900


def _signing_key():
    """A mock OAuth2SigningKey row backed by real EC P-256 key material."""
    key = ECKey.generate_key("P-256")
    signing_key = MagicMock()
    signing_key.kid = key.thumbprint()
    signing_key.private_key_pem = key.as_pem(private=True).decode("ascii")
    signing_key.algorithm = "ES256"
    return signing_key


def _jwks_for(*keys) -> list[dict]:
    return [signing_keys.public_jwk(key) for key in keys]


def _mint(signing_key, *, subject=USER_ID, audience=RESOURCE, issuer=ISSUER, scope="openid mcp", expires_in=ACCESS_TTL):
    return create_access_token_jwt(
        subject=subject,
        client_id="abc",
        scope=scope,
        audience=audience,
        issuer=issuer,
        signing_key=signing_key,
        expires_in=expires_in,
    )


def _settings():
    settings = MagicMock()
    settings.oauth2_issuer = ISSUER
    return settings


def _connection(*, headers=None, session=None):
    """Build a stand-in ``ASGIConnection`` with just the pieces the guard
    touches — mirrors tests/test_auth_guards_client_ip.py."""
    connection = MagicMock()
    connection.headers = headers or {}
    connection.session = session if session is not None else {}
    connection.scope = {"state": {}, "client": ("1.2.3.4", 1234)}
    connection.base_url = "http://localhost:8000/"

    db_session = AsyncMock()
    context_manager = AsyncMock()
    context_manager.__aenter__ = AsyncMock(return_value=db_session)
    context_manager.__aexit__ = AsyncMock(return_value=None)
    connection.app.state.session_maker_class = MagicMock(return_value=context_manager)
    return connection, db_session


def _route_handler(*guards):
    handler = MagicMock()
    handler.guards = [auth_guard, *guards]
    return handler


async def _run_guard(
    guards,
    *,
    headers=None,
    session=None,
    jwks=None,
    revoked=False,
    user="active",
    permissions=None,
):
    """Invoke ``auth_guard`` with all bearer-JWT collaborators patched."""
    connection, db_session = _connection(headers=headers, session=session)
    if user == "active":
        user = MagicMock(is_active=True)
    elif user == "inactive":
        user = MagicMock(is_active=False)
    db_session.get = AsyncMock(return_value=user)

    granted = permissions if permissions is not None else UserPermissions(
        user_id=USER_ID, roles=set(), permissions={"manage-pages"}
    )

    with patch("skrift.auth.bearer.get_settings", return_value=_settings()), \
         patch("skrift.auth.bearer.oauth2_signing_key_service") as mock_keys, \
         patch("skrift.auth.bearer.oauth2_service") as mock_oauth2, \
         patch("skrift.auth.bearer.get_user_permissions", new=AsyncMock(return_value=granted)), \
         patch("skrift.auth.services.get_user_permissions", new=AsyncMock(return_value=granted)):
        mock_keys.list_jwks_keys = AsyncMock(return_value=jwks if jwks is not None else [])
        mock_oauth2.is_token_revoked = AsyncMock(return_value=revoked)
        await auth_guard(connection, _route_handler(*guards))
    return db_session


def _www_authenticate(excinfo) -> str:
    return (excinfo.value.headers or {}).get("WWW-Authenticate", "")


class TestJwtShapeDisambiguation:
    def test_compact_jws_is_jwt_shaped(self):
        assert is_jwt_shaped("eyJhbGciOi.eyJzdWIiOi.c2ln")

    def test_api_key_prefix_is_never_jwt_shaped(self):
        assert not is_jwt_shaped("sk_live_abc123")
        assert not is_jwt_shaped("sk_a.b.c")

    def test_wrong_segment_counts_are_not_jwt_shaped(self):
        for token in ["", "abc", "a.b", "a.b.c.d"]:
            assert not is_jwt_shaped(token)


class TestBearerJwtHappyPath:
    @pytest.mark.asyncio
    async def test_valid_jwt_passes_guard_and_permission_check(self):
        key = _signing_key()
        await _run_guard(
            [BearerJWTAuth(audience=RESOURCE, scopes=["mcp"]), Permission("manage-pages")],
            headers={"authorization": f"Bearer {_mint(key)}"},
            jwks=_jwks_for(key),
        )

    @pytest.mark.asyncio
    async def test_permission_requirements_still_enforced_for_jwt_identity(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException):
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE), Permission("manage-users")],
                headers={"authorization": f"Bearer {_mint(key)}"},
                jwks=_jwks_for(key),
                permissions=UserPermissions(user_id=USER_ID, roles=set(), permissions={"manage-pages"}),
            )

    @pytest.mark.asyncio
    async def test_jwt_only_route_accepts_valid_jwt(self):
        key = _signing_key()
        await _run_guard(
            [BearerJWTOnly(audience=RESOURCE)],
            headers={"authorization": f"Bearer {_mint(key)}"},
            jwks=_jwks_for(key),
        )


class TestBearerJwtRejections:
    @pytest.mark.asyncio
    async def test_expired_jwt_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key, expires_in=-10)}"},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_wrong_audience_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key, audience=OTHER_RESOURCE)}"},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_missing_audience_claim_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key, audience=None)}"},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_tampered_jwt_rejected_as_invalid_token(self):
        import base64
        import json

        key = _signing_key()
        token = _mint(key)
        header_b64, payload_b64, signature_b64 = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        claims["sub"] = USER_ID.replace("42", "43")
        tampered_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {header_b64}.{tampered_payload}.{signature_b64}"},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_wrong_issuer_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key, issuer='https://evil.example.com')}"},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_missing_required_scope_rejected_as_insufficient_scope(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE, scopes=["admin"])],
                headers={"authorization": f"Bearer {_mint(key, scope='openid mcp')}"},
                jwks=_jwks_for(key),
            )
        challenge = _www_authenticate(excinfo)
        assert 'error="insufficient_scope"' in challenge
        assert 'scope="admin"' in challenge

    @pytest.mark.asyncio
    async def test_revoked_jti_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key)}"},
                jwks=_jwks_for(key),
                revoked=True,
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_unknown_subject_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key)}"},
                jwks=_jwks_for(key),
                user=None,
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_inactive_subject_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key)}"},
                jwks=_jwks_for(key),
                user="inactive",
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_malformed_subject_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key, subject='not-a-uuid')}"},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_empty_jwks_rejected_as_invalid_token(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key)}"},
                jwks=[],
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)

    @pytest.mark.asyncio
    async def test_missing_bearer_on_jwt_only_route_gets_bearer_challenge(self):
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard([BearerJWTOnly(audience=RESOURCE)])
        assert _www_authenticate(excinfo).startswith("Bearer")


class TestSchemeCollision:
    @pytest.mark.asyncio
    async def test_api_key_bearer_still_routes_to_api_key_path(self):
        granted = UserPermissions(user_id=USER_ID, roles=set(), permissions={"manage-pages"})
        connection, _ = _connection(headers={"authorization": "Bearer sk_test_key"})
        resolve_api_key = AsyncMock(return_value=(USER_ID, granted))
        with patch("skrift.auth.guards._resolve_api_key_permissions", resolve_api_key):
            await auth_guard(
                connection,
                _route_handler(APIKeyAuth(), Permission("manage-pages")),
            )
        resolve_api_key.assert_awaited_once()
        assert resolve_api_key.await_args.args[1] == "sk_test_key"

    @pytest.mark.asyncio
    async def test_jwt_does_not_satisfy_api_key_only_route(self):
        key = _signing_key()
        connection, _ = _connection(headers={"authorization": f"Bearer {_mint(key)}"})
        resolve_api_key = AsyncMock(return_value=(None, None))
        with patch("skrift.auth.guards._resolve_api_key_permissions", resolve_api_key), \
             pytest.raises(NotAuthorizedException):
            await auth_guard(connection, _route_handler(APIKeyOnly()))
        resolve_api_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_api_key_does_not_satisfy_jwt_only_route(self):
        connection, _ = _connection(headers={"authorization": "Bearer sk_test_key"})
        resolve_api_key = AsyncMock(return_value=(USER_ID, MagicMock()))
        with patch("skrift.auth.guards._resolve_api_key_permissions", resolve_api_key), \
             pytest.raises(NotAuthorizedException):
            await auth_guard(connection, _route_handler(BearerJWTOnly(audience=RESOURCE)))
        resolve_api_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mixed_route_routes_each_scheme_correctly(self):
        key = _signing_key()
        await _run_guard(
            [BearerJWTAuth(audience=RESOURCE), APIKeyAuth(), Permission("manage-pages")],
            headers={"authorization": f"Bearer {_mint(key)}"},
            jwks=_jwks_for(key),
        )


class TestSessionInterplay:
    @pytest.mark.asyncio
    async def test_session_user_allowed_on_jwt_compatible_route(self):
        await _run_guard(
            [BearerJWTAuth(audience=RESOURCE), Permission("manage-pages")],
            session={SESSION_USER_ID: USER_ID},
        )

    @pytest.mark.asyncio
    async def test_session_user_rejected_on_jwt_only_route(self):
        with pytest.raises(NotAuthorizedException):
            await _run_guard(
                [BearerJWTOnly(audience=RESOURCE)],
                session={SESSION_USER_ID: USER_ID},
            )

    @pytest.mark.asyncio
    async def test_invalid_jwt_does_not_fall_back_to_session(self):
        key = _signing_key()
        with pytest.raises(NotAuthorizedException) as excinfo:
            await _run_guard(
                [BearerJWTAuth(audience=RESOURCE)],
                headers={"authorization": f"Bearer {_mint(key, expires_in=-10)}"},
                session={SESSION_USER_ID: USER_ID},
                jwks=_jwks_for(key),
            )
        assert 'error="invalid_token"' in _www_authenticate(excinfo)
