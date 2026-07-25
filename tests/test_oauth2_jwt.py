"""Phase 1 MCP identity provider — ES256 JWT access tokens, JWKS publication,
RFC 8707 resource→aud propagation, and RFC 8414 discovery.

Auth codes and refresh tokens stay HMAC (`skrift.auth.tokens`); only the
access-token format changes to an asymmetric JWT verifiable via /oauth/jwks.
"""

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from joserfc import jwt as jose_jwt
from joserfc.jwk import ECKey
from litestar.datastructures import FormMultiDict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.auth.jwt_tokens import create_access_token_jwt, verify_access_token_jwt
from skrift.auth.tokens import create_signed_token, verify_signed_token
from skrift.config import SecurityHeadersConfig
from skrift.controllers.oauth2 import (
    AUTH_CODE_TTL,
    REFRESH_TOKEN_TTL,
    OAuth2Controller,
)
from skrift.db.base import Base
from skrift.db.models import OAuth2SigningKey  # noqa: F401  (registers table)
from skrift.db.services import oauth2_signing_key_service as signing_keys


SECRET = "test-secret-key"
ISSUER = "http://localhost:8000"
RESOURCE = "https://mcp.example.com/sse"
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


def _settings(allowed_resources: list[str] | None = None):
    settings = MagicMock()
    settings.secret_key = SECRET
    settings.security_headers = SecurityHeadersConfig()
    settings.oauth2_issuer = ISSUER
    settings.oauth2_access_token_ttl = ACCESS_TTL
    settings.oauth2_allowed_resources = allowed_resources or []
    return settings


def _client(client_id="abc", client_secret="secret", allowed_scopes=None):
    from skrift.auth.client_secret import hash_client_secret

    client = MagicMock()
    client.client_id = client_id
    client.client_secret = hash_client_secret(client_secret) if client_secret else ""
    client.display_name = "Test App"
    client.is_active = True
    client.redirect_uri_list = ["http://localhost/cb"]
    client.allowed_scope_list = allowed_scopes or []
    return client


def _pkce_pair():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _mint(signing_key, *, audience=RESOURCE, issuer=ISSUER, scope="openid email profile", expires_in=ACCESS_TTL):
    return create_access_token_jwt(
        subject=USER_ID,
        client_id="abc",
        scope=scope,
        audience=audience,
        issuer=issuer,
        signing_key=signing_key,
        expires_in=expires_in,
    )


class TestJwtRoundTrip:
    def test_create_verify_round_trip(self):
        key = _signing_key()
        token = _mint(key)
        claims = verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE)
        assert claims is not None
        assert claims["sub"] == USER_ID
        assert claims["client_id"] == "abc"
        assert claims["scope"] == "openid email profile"
        assert claims["aud"] == RESOURCE
        assert claims["iss"] == ISSUER
        assert claims["jti"]
        assert claims["exp"] > claims["iat"]

    def test_wrong_audience_rejected(self):
        key = _signing_key()
        token = _mint(key)
        assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=OTHER_RESOURCE) is None

    def test_missing_audience_claim_rejected_when_audience_expected(self):
        key = _signing_key()
        token = _mint(key, audience=None)
        assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE) is None

    def test_audience_none_skips_audience_check(self):
        key = _signing_key()
        token = _mint(key, audience=None)
        claims = verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=None)
        assert claims is not None
        assert "aud" not in claims

    def test_expired_rejected(self):
        key = _signing_key()
        token = _mint(key, expires_in=-10)
        assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE) is None

    def test_wrong_issuer_rejected(self):
        key = _signing_key()
        token = _mint(key, issuer="https://evil.example.com")
        assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE) is None

    def test_tampered_payload_rejected(self):
        key = _signing_key()
        token = _mint(key)
        header_b64, payload_b64, signature_b64 = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        claims["sub"] = "attacker"
        tampered_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        tampered = f"{header_b64}.{tampered_payload}.{signature_b64}"
        assert verify_access_token_jwt(tampered, jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE) is None

    def test_wrong_key_rejected(self):
        signer = _signing_key()
        other = _signing_key()
        token = _mint(signer)
        assert verify_access_token_jwt(token, jwks=_jwks_for(other), issuer=ISSUER, audience=RESOURCE) is None

    def test_garbage_token_rejected(self):
        key = _signing_key()
        for garbage in ["", "abc", "a.b", "a.b.c", "a.b.c.d"]:
            assert verify_access_token_jwt(garbage, jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE) is None

    def test_empty_jwks_rejected(self):
        key = _signing_key()
        token = _mint(key)
        assert verify_access_token_jwt(token, jwks=[], issuer=ISSUER, audience=RESOURCE) is None


def _mint_with_typ(signing_key, typ):
    """Sign otherwise-valid access-token claims under an arbitrary header typ."""
    header = {"alg": "ES256", "kid": signing_key.kid}
    if typ is not None:
        header["typ"] = typ
    issued_at = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": USER_ID,
        "client_id": "abc",
        "scope": "openid",
        "iat": issued_at,
        "exp": issued_at + ACCESS_TTL,
        "jti": "typ-test-jti",
    }
    private_key = ECKey.import_key(signing_key.private_key_pem)
    return jose_jwt.encode(header, claims, private_key, algorithms=["ES256"])


class TestJwtTypEnforcement:
    """RFC 9068 §4 — only `at+jwt` tokens may pass as access tokens, so any
    other JWT kind signed with the same rotating key can never be confused
    into one."""

    def test_at_jwt_typ_accepted(self):
        key = _signing_key()
        for accepted in ("at+jwt", "application/at+jwt", "AT+JWT"):
            token = _mint_with_typ(key, accepted)
            assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=None) is not None, accepted

    def test_wrong_typ_rejected(self):
        key = _signing_key()
        for wrong in ("JWT", "logout+jwt", "id_token+jwt", ""):
            token = _mint_with_typ(key, wrong)
            assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=None) is None, wrong

    def test_missing_typ_rejected(self):
        key = _signing_key()
        token = _mint_with_typ(key, None)
        assert verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=None) is None

    def test_create_access_token_jwt_sets_at_jwt_typ(self):
        key = _signing_key()
        token = _mint(key)
        header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
        assert header["typ"] == "at+jwt"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class TestSigningKeyRotation:
    @pytest.mark.asyncio
    async def test_token_signed_by_retired_key_verifies_until_pruned(self, db_session):
        original = await signing_keys.get_or_create_active_key(db_session)
        token = _mint(original)

        await signing_keys.rotate(db_session)
        jwks = await signing_keys.list_jwks_keys(db_session)
        assert verify_access_token_jwt(token, jwks=jwks, issuer=ISSUER, audience=RESOURCE) is not None

        future = datetime.now(tz=timezone.utc) + timedelta(seconds=ACCESS_TTL + 120)
        await signing_keys.prune_expired(db_session, now=future, access_token_ttl=ACCESS_TTL, clock_skew=60)
        jwks = await signing_keys.list_jwks_keys(db_session)
        assert verify_access_token_jwt(token, jwks=jwks, issuer=ISSUER, audience=RESOURCE) is None


class TestJwksRoute:
    @pytest.mark.asyncio
    async def test_jwks_route_returns_active_key(self, db_session):
        controller = OAuth2Controller(owner=MagicMock())
        result = await OAuth2Controller.jwks.fn(controller, db_session)

        assert result.status_code == 200
        keys = result.content["keys"]
        assert len(keys) == 1
        active = await signing_keys.get_or_create_active_key(db_session)
        assert keys[0]["kid"] == active.kid
        assert keys[0]["kty"] == "EC"
        assert keys[0]["alg"] == "ES256"
        assert "d" not in keys[0]


class _MultiValueParams(dict):
    """Query/form params stub exposing Litestar's multi-dict ``getall``."""

    def __init__(self, base: dict, resources: list[str]):
        super().__init__(base)
        self._resources = resources

    def getall(self, key, default=None):
        if key == "resource":
            return list(self._resources)
        value = super().get(key)
        if value is None:
            return default if default is not None else []
        return [value]


def _authorize_params(**overrides):
    params = {
        "client_id": "abc",
        "redirect_uri": "http://localhost/cb",
        "response_type": "code",
        "state": "xyz",
        "scope": "openid",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


async def _authorize_get(query, *, session=None, settings=None):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.query_params = query
    request.session = session if session is not None else {}
    db_session = AsyncMock()
    with patch("skrift.controllers.oauth2.get_settings", return_value=settings or _settings()), \
         patch("skrift.controllers.oauth2.oauth2_consent_service") as mock_consent, \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=_client())
        mock_consent.find_grant = AsyncMock(return_value=None)
        return await OAuth2Controller.authorize_get.fn(controller, request, db_session), request


class TestAuthorizeResourceValidation:
    @pytest.mark.asyncio
    async def test_missing_resource_is_allowed(self):
        result, _ = await _authorize_get(_authorize_params())
        from litestar.response import Redirect

        assert isinstance(result, Redirect)

    @pytest.mark.asyncio
    async def test_relative_resource_rejected_as_invalid_target(self):
        result, _ = await _authorize_get(_authorize_params(resource="not-a-uri"))
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_resource_with_fragment_rejected(self):
        result, _ = await _authorize_get(_authorize_params(resource="https://mcp.example.com/sse#frag"))
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_multiple_resources_rejected(self):
        query = _MultiValueParams(_authorize_params(resource=RESOURCE), [RESOURCE, OTHER_RESOURCE])
        result, _ = await _authorize_get(query)
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_resource_outside_allowlist_rejected(self):
        result, _ = await _authorize_get(
            _authorize_params(resource=OTHER_RESOURCE),
            settings=_settings(allowed_resources=[RESOURCE]),
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_allowlisted_resource_accepted_and_stored_in_session(self):
        session = {"user_id": USER_ID}
        result, _ = await _authorize_get(
            _authorize_params(resource=RESOURCE),
            session=session,
            settings=_settings(allowed_resources=[RESOURCE]),
        )
        from litestar.response import Template as TemplateResponse

        assert isinstance(result, TemplateResponse)
        assert session["oauth_authorize"]["resource"] == RESOURCE

    @pytest.mark.asyncio
    async def test_resource_survives_login_redirect(self):
        result, _ = await _authorize_get(_authorize_params(resource=RESOURCE))
        from litestar.response import Redirect

        assert isinstance(result, Redirect)
        login = urlsplit(result.url)
        next_url = parse_qs(login.query)["next"][0]
        authorize_params = parse_qs(urlsplit(next_url).query)
        assert authorize_params["resource"] == [RESOURCE]


class TestAuthorizePostCarriesResource:
    @pytest.mark.asyncio
    async def test_approved_code_embeds_resource(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.session = {
            "user_id": USER_ID,
            "user_email": "u@example.com",
            "user_name": "U User",
            "user_picture_url": "",
            "oauth_authorize": {
                "client_id": "abc",
                "redirect_uri": "http://localhost/cb",
                "state": "xyz",
                "scope": "openid",
                "code_challenge": "challenge",
                "resource": RESOURCE,
            },
        }
        request.form = AsyncMock(
            return_value=FormMultiDict([("action", "allow"), ("scope", "openid")])
        )
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
             patch("skrift.controllers.oauth2.oauth2_consent_service") as mock_consent, \
             patch("skrift.controllers.oauth2.get_settings", return_value=_settings()):
            mock_consent.upsert_grant = AsyncMock()
            result = await OAuth2Controller.authorize_post.fn(controller, request, db_session)

        code = parse_qs(urlsplit(result.url).query)["code"][0]
        payload = verify_signed_token(code, SECRET)
        assert payload["resource"] == RESOURCE


def _auth_code(*, resource="", challenge=None):
    _, default_challenge = _pkce_pair()
    payload = {
        "type": "code",
        "user_id": USER_ID,
        "email": "u@example.com",
        "name": "U User",
        "picture_url": "",
        "client_id": "abc",
        "redirect_uri": "http://localhost/cb",
        "scope": "openid",
        "code_challenge": challenge if challenge is not None else default_challenge,
    }
    if resource:
        payload["resource"] = resource
    return create_signed_token(payload, SECRET, AUTH_CODE_TTL)


async def _exchange(form_data, *, settings=None, signing_key=None):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.form = AsyncMock(return_value=form_data)
    request.base_url = "http://localhost:8000/"
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=settings or _settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc, \
         patch("skrift.controllers.oauth2.oauth2_signing_key_service") as mock_keys:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=_client())
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        mock_svc.is_family_revoked = AsyncMock(return_value=False)
        mock_svc.revoke_token = AsyncMock()
        mock_svc.revoke_family = AsyncMock()
        mock_svc.mark_client_used = AsyncMock()
        mock_keys.get_or_create_active_key = AsyncMock(return_value=signing_key or _signing_key())
        return await OAuth2Controller.token_exchange.fn(controller, request, db_session)


def _code_grant_form(code, **overrides):
    verifier, _ = _pkce_pair()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://localhost/cb",
        "client_id": "abc",
        "client_secret": "secret",
        "code_verifier": verifier,
    }
    form.update(overrides)
    return form


class TestTokenEndpointIssuesJwt:
    @pytest.mark.asyncio
    async def test_code_resource_becomes_audience(self):
        key = _signing_key()
        result = await _exchange(_code_grant_form(_auth_code(resource=RESOURCE)), signing_key=key)

        assert result.status_code == 200
        body = result.content
        assert body["expires_in"] == ACCESS_TTL
        claims = verify_access_token_jwt(
            body["access_token"], jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE
        )
        assert claims is not None
        assert claims["sub"] == USER_ID
        assert claims["client_id"] == "abc"
        assert claims["scope"] == "openid"
        assert claims["aud"] == RESOURCE
        refresh_payload = verify_signed_token(body["refresh_token"], SECRET)
        assert refresh_payload["resource"] == RESOURCE

    @pytest.mark.asyncio
    async def test_no_resource_yields_jwt_without_audience(self):
        key = _signing_key()
        result = await _exchange(_code_grant_form(_auth_code()), signing_key=key)

        assert result.status_code == 200
        claims = verify_access_token_jwt(
            result.content["access_token"], jwks=_jwks_for(key), issuer=ISSUER, audience=None
        )
        assert claims is not None
        assert "aud" not in claims

    @pytest.mark.asyncio
    async def test_token_endpoint_resource_narrows_unrestricted_grant(self):
        key = _signing_key()
        result = await _exchange(
            _code_grant_form(_auth_code(), resource=RESOURCE), signing_key=key
        )

        assert result.status_code == 200
        claims = verify_access_token_jwt(
            result.content["access_token"], jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE
        )
        assert claims is not None

    @pytest.mark.asyncio
    async def test_token_endpoint_resource_mismatch_rejected(self):
        result = await _exchange(
            _code_grant_form(_auth_code(resource=RESOURCE), resource=OTHER_RESOURCE)
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_token_endpoint_invalid_resource_rejected(self):
        result = await _exchange(_code_grant_form(_auth_code(), resource="not-a-uri"))
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_token_endpoint_enforces_allowlist(self):
        result = await _exchange(
            _code_grant_form(_auth_code(), resource=OTHER_RESOURCE),
            settings=_settings(allowed_resources=[RESOURCE]),
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"


class TestRefreshKeepsAudience:
    def _refresh_token(self, *, resource=""):
        payload = {
            "type": "refresh",
            "user_id": USER_ID,
            "client_id": "abc",
            "scope": "openid",
            "family_id": "fam-aud",
        }
        if resource:
            payload["resource"] = resource
        return create_signed_token(payload, SECRET, REFRESH_TOKEN_TTL)

    def _refresh_form(self, refresh, **overrides):
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "abc",
            "client_secret": "secret",
        }
        form.update(overrides)
        return form

    @pytest.mark.asyncio
    async def test_refreshed_access_token_keeps_audience(self):
        key = _signing_key()
        result = await _exchange(
            self._refresh_form(self._refresh_token(resource=RESOURCE)), signing_key=key
        )

        assert result.status_code == 200
        body = result.content
        claims = verify_access_token_jwt(
            body["access_token"], jwks=_jwks_for(key), issuer=ISSUER, audience=RESOURCE
        )
        assert claims is not None
        assert claims["aud"] == RESOURCE
        new_refresh_payload = verify_signed_token(body["refresh_token"], SECRET)
        assert new_refresh_payload["resource"] == RESOURCE

    @pytest.mark.asyncio
    async def test_refresh_resource_mismatch_rejected(self):
        result = await _exchange(
            self._refresh_form(self._refresh_token(resource=RESOURCE), resource=OTHER_RESOURCE)
        )
        assert result.status_code == 400
        assert result.content["error"] == "invalid_target"

    @pytest.mark.asyncio
    async def test_legacy_refresh_without_resource_still_works(self):
        key = _signing_key()
        result = await _exchange(self._refresh_form(self._refresh_token()), signing_key=key)

        assert result.status_code == 200
        claims = verify_access_token_jwt(
            result.content["access_token"], jwks=_jwks_for(key), issuer=ISSUER, audience=None
        )
        assert claims is not None
        assert "aud" not in claims


async def _userinfo(token, jwks, *, user="default", revoked=False, email_verified=True):
    if user == "default":
        user = MagicMock(email="u@example.com", picture_url="https://x/p.png")
        user.name = "U User"
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.headers = {"authorization": f"Bearer {token}"}
    request.base_url = "http://localhost:8000/"
    db_session = AsyncMock()
    db_session.get = AsyncMock(return_value=user)

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc, \
         patch("skrift.controllers.oauth2.oauth_service") as mock_oauth_svc, \
         patch("skrift.controllers.oauth2.oauth2_signing_key_service") as mock_keys:
        mock_svc.is_token_revoked = AsyncMock(return_value=revoked)
        mock_oauth_svc.is_email_verified_for_user = AsyncMock(return_value=email_verified)
        mock_keys.list_jwks_keys = AsyncMock(return_value=jwks)
        return await OAuth2Controller.userinfo.fn(controller, request, db_session)


class TestUserinfoWithJwt:
    @pytest.mark.asyncio
    async def test_full_scopes_return_profile_from_database(self):
        key = _signing_key()
        result = await _userinfo(_mint(key), _jwks_for(key))

        assert result.status_code == 200
        assert result.content == {
            "sub": USER_ID,
            "email": "u@example.com",
            "email_verified": True,
            "name": "U User",
            "picture": "https://x/p.png",
        }

    @pytest.mark.asyncio
    async def test_openid_only_returns_sub(self):
        key = _signing_key()
        result = await _userinfo(_mint(key, scope="openid"), _jwks_for(key))
        assert result.content == {"sub": USER_ID}

    @pytest.mark.asyncio
    async def test_revoked_jwt_rejected(self):
        key = _signing_key()
        result = await _userinfo(_mint(key), _jwks_for(key), revoked=True)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_jwt_from_other_issuer_rejected(self):
        key = _signing_key()
        result = await _userinfo(_mint(key, issuer="https://evil.example.com"), _jwks_for(key))
        assert result.status_code == 401


async def _revoke(token, jwks):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.form = AsyncMock(return_value={"token": token})
    request.base_url = "http://localhost:8000/"
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc, \
         patch("skrift.controllers.oauth2.oauth2_signing_key_service") as mock_keys:
        mock_svc.is_token_revoked = AsyncMock(return_value=False)
        mock_svc.revoke_token = AsyncMock()
        mock_keys.list_jwks_keys = AsyncMock(return_value=jwks)
        result = await OAuth2Controller.revoke.fn(controller, request, db_session)
    return result, mock_svc


class TestRevokeWithJwt:
    @pytest.mark.asyncio
    async def test_jwt_access_token_revocation_stores_its_jti(self):
        key = _signing_key()
        token = _mint(key)
        claims = verify_access_token_jwt(token, jwks=_jwks_for(key), issuer=ISSUER, audience=None)

        result, mock_svc = await _revoke(token, _jwks_for(key))

        assert result.status_code == 200
        mock_svc.revoke_token.assert_awaited_once()
        _, stored_jti, stored_type, stored_expiry = mock_svc.revoke_token.await_args.args
        assert stored_jti == claims["jti"]
        assert stored_type == "access"
        assert stored_expiry == datetime.fromtimestamp(claims["exp"], tz=timezone.utc)

    @pytest.mark.asyncio
    async def test_revoked_jwt_no_longer_passes_userinfo(self):
        """End-to-end shape of the connector 'disconnect' path: once the JWT's
        jti is on the revocation list, the token stops working."""
        key = _signing_key()
        token = _mint(key)

        result = await _userinfo(token, _jwks_for(key), revoked=True)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_foreign_issuer_jwt_returns_200_without_storing_revocation(self):
        key = _signing_key()
        token = _mint(key, issuer="https://evil.example.com")

        result, mock_svc = await _revoke(token, _jwks_for(key))

        assert result.status_code == 200
        mock_svc.revoke_token.assert_not_awaited()


async def _introspect(token, jwks, *, client=None, revoked=False):
    controller = OAuth2Controller(owner=MagicMock())
    request = MagicMock()
    request.form = AsyncMock(return_value={
        "token": token,
        "client_id": (client or _client()).client_id,
        "client_secret": "secret",
    })
    request.base_url = "http://localhost:8000/"
    db_session = AsyncMock()

    with patch("skrift.controllers.oauth2.get_settings", return_value=_settings()), \
         patch("skrift.controllers.oauth2.oauth2_service") as mock_svc, \
         patch("skrift.controllers.oauth2.oauth2_signing_key_service") as mock_keys:
        mock_svc.get_client_by_client_id = AsyncMock(return_value=client or _client())
        mock_svc.is_token_revoked = AsyncMock(return_value=revoked)
        mock_keys.list_jwks_keys = AsyncMock(return_value=jwks)
        return await OAuth2Controller.introspect.fn(controller, request, db_session)


class TestIntrospectWithJwt:
    @pytest.mark.asyncio
    async def test_issuing_client_sees_full_jwt_details(self):
        key = _signing_key()
        result = await _introspect(_mint(key), _jwks_for(key))

        assert result.status_code == 200
        body = result.content
        assert body["active"] is True
        assert body["token_type"] == "access"
        assert body["sub"] == USER_ID
        assert body["scope"] == "openid email profile"
        assert body["client_id"] == "abc"
        assert body["aud"] == RESOURCE
        assert body["exp"] > time.time()

    @pytest.mark.asyncio
    async def test_other_client_sees_minimal_jwt_details(self):
        key = _signing_key()
        result = await _introspect(_mint(key), _jwks_for(key), client=_client(client_id="nosy"))

        body = result.content
        assert body["active"] is True
        assert "sub" not in body
        assert "scope" not in body
        assert "aud" not in body

    @pytest.mark.asyncio
    async def test_revoked_jwt_is_inactive(self):
        key = _signing_key()
        result = await _introspect(_mint(key), _jwks_for(key), revoked=True)
        assert result.content["active"] is False

    @pytest.mark.asyncio
    async def test_hmac_refresh_token_still_introspects(self):
        refresh = create_signed_token({
            "type": "refresh",
            "user_id": USER_ID,
            "client_id": "abc",
            "scope": "openid",
            "family_id": "fam-x",
        }, SECRET, REFRESH_TOKEN_TTL)
        result = await _introspect(refresh, [])

        body = result.content
        assert body["active"] is True
        assert body["token_type"] == "refresh"
        assert body["sub"] == USER_ID


class TestDiscoveryDocuments:
    async def _fetch(self, route_name):
        from skrift.controllers.sitemap import SitemapController

        settings = MagicMock()
        settings.oauth2_enabled = True
        settings.oauth2_issuer = ""
        settings.oauth2_dynamic_registration_enabled = False

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()
        request.base_url = "http://localhost:8000/"

        with patch("skrift.config.get_settings", return_value=settings):
            handler = getattr(SitemapController, route_name)
            return await handler.fn(controller, request)

    @pytest.mark.asyncio
    async def test_both_documents_served_and_equal(self):
        openid = await self._fetch("openid_configuration")
        rfc8414 = await self._fetch("oauth_authorization_server")

        assert openid.status_code == 200
        assert rfc8414.status_code == 200
        assert openid.content == rfc8414.content

    @pytest.mark.asyncio
    async def test_metadata_contains_jwks_uri_and_registry_scopes(self):
        result = await self._fetch("oauth_authorization_server")
        body = result.content

        assert body["issuer"] == ISSUER
        assert body["jwks_uri"] == f"{ISSUER}/oauth/jwks"
        assert set(body["scopes_supported"]) >= {"openid", "profile", "email"}
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert "token_endpoint_auth_methods_supported" in body
        assert "registration_endpoint" not in body

    @pytest.mark.asyncio
    async def test_configured_issuer_overrides_request_base_url(self):
        from skrift.controllers.sitemap import SitemapController

        settings = MagicMock()
        settings.oauth2_enabled = True
        settings.oauth2_issuer = "https://id.example.com"
        settings.oauth2_dynamic_registration_enabled = False

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()
        request.base_url = "http://localhost:8000/"

        with patch("skrift.config.get_settings", return_value=settings):
            result = await SitemapController.oauth_authorization_server.fn(controller, request)

        assert result.content["issuer"] == "https://id.example.com"
        assert result.content["jwks_uri"] == "https://id.example.com/oauth/jwks"
