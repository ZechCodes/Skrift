"""Tests for the OAuth2 Authorization Server controller."""

import base64
import hashlib
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from litestar.response import Redirect, Template as TemplateResponse

from skrift.auth.scopes import SCOPE_DEFINITIONS, register_scope, get_scope_definition
from skrift.auth.tokens import create_signed_token, verify_signed_token
from skrift.controllers.oauth2 import (
    OAuth2Controller,
    _verify_pkce,
    _json_error,
    verify_oauth_token,
    AUTH_CODE_TTL,
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
)


SECRET = "test-secret-key"


def _make_settings():
    """Create a mock settings object."""
    settings = MagicMock()
    settings.secret_key = SECRET
    return settings


def _mock_client(
    client_id="abc",
    client_secret="secret",
    redirect_uris=None,
    allowed_scopes=None,
    is_active=True,
):
    """Create a mock OAuth2Client model."""
    mc = MagicMock()
    mc.client_id = client_id
    mc.client_secret = client_secret
    mc.display_name = f"Test App ({client_id})"
    mc.is_active = is_active
    mc.redirect_uri_list = redirect_uris or ["http://localhost/cb"]
    mc.allowed_scope_list = allowed_scopes or []
    return mc


def _generate_pkce():
    """Generate a PKCE code_verifier and code_challenge pair."""
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


class TestVerifyPkce:
    def test_valid_pkce(self):
        verifier, challenge = _generate_pkce()
        assert _verify_pkce(verifier, challenge) is True

    def test_invalid_pkce(self):
        _, challenge = _generate_pkce()
        assert _verify_pkce("wrong-verifier", challenge) is False


class TestJsonError:
    def test_returns_error_response(self):
        resp = _json_error("invalid_request", "Bad thing happened")
        assert resp.status_code == 400
        assert resp.content["error"] == "invalid_request"
        assert resp.content["error_description"] == "Bad thing happened"


class TestJti:
    def test_tokens_include_jti(self):
        token = create_signed_token({"type": "access"}, SECRET, 300)
        payload = verify_signed_token(token, SECRET)
        assert "jti" in payload
        assert len(payload["jti"]) == 32  # uuid4().hex

    def test_each_token_has_unique_jti(self):
        t1 = create_signed_token({"type": "access"}, SECRET, 300)
        t2 = create_signed_token({"type": "access"}, SECRET, 300)
        p1 = verify_signed_token(t1, SECRET)
        p2 = verify_signed_token(t2, SECRET)
        assert p1["jti"] != p2["jti"]


class TestVerifyOAuthToken:
    @pytest.mark.asyncio
    async def test_valid_token(self):
        token = create_signed_token({"type": "access"}, SECRET, 300)
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=False)
            payload = await verify_oauth_token(token, SECRET, db_session)

        assert payload is not None
        assert payload["type"] == "access"

    @pytest.mark.asyncio
    async def test_revoked_token(self):
        token = create_signed_token({"type": "access"}, SECRET, 300)
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=True)
            payload = await verify_oauth_token(token, SECRET, db_session)

        assert payload is None

    @pytest.mark.asyncio
    async def test_invalid_token(self):
        db_session = AsyncMock()
        payload = await verify_oauth_token("bogus", SECRET, db_session)
        assert payload is None


class TestAuthorizeGet:
    @pytest.mark.asyncio
    async def test_requires_code_response_type(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {"response_type": "token", "client_id": "abc"}
        request.session = {}
        db_session = AsyncMock()

        result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_unknown_client(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "unknown",
            "redirect_uri": "http://localhost/cb",
        }
        request.session = {}
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=None)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_invalid_redirect_uri(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://evil.com/cb",
        }
        request.session = {}
        db_session = AsyncMock()
        client = _mock_client(redirect_uris=["http://localhost/cb"])

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_public_client_requires_pkce(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "state": "xyz",
            "scope": "openid",
            "code_challenge": "",
            "code_challenge_method": "",
        }
        request.session = {}
        db_session = AsyncMock()
        client = _mock_client(client_secret="", redirect_uris=["http://localhost/cb"])

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_unknown_scope(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "state": "xyz",
            "scope": "openid bogus_scope",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }
        request.session = {}
        db_session = AsyncMock()
        client = _mock_client(redirect_uris=["http://localhost/cb"])

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)
        assert result.status_code == 400
        assert "Unknown scope" in result.content["error_description"]

    @pytest.mark.asyncio
    async def test_rejects_disallowed_scope(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "state": "xyz",
            "scope": "openid email",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }
        request.session = {}
        db_session = AsyncMock()
        # Client only allows openid scope
        client = _mock_client(redirect_uris=["http://localhost/cb"], allowed_scopes=["openid"])

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)
        assert result.status_code == 400
        assert "not allowed" in result.content["error_description"]

    @pytest.mark.asyncio
    async def test_redirects_to_login_if_not_authenticated(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "state": "xyz",
            "scope": "openid",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }
        request.session = {}  # No user_id
        db_session = AsyncMock()
        client = _mock_client(redirect_uris=["http://localhost/cb"])

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)

        assert isinstance(result, Redirect)
        assert "/auth/login" in result.url

    @pytest.mark.asyncio
    async def test_shows_consent_screen_when_logged_in(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {
            "response_type": "code",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "state": "xyz",
            "scope": "openid profile",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
        }
        session = {"user_id": "user-123"}
        request.session = session
        db_session = AsyncMock()
        client = _mock_client(redirect_uris=["http://localhost/cb"])

        with patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.authorize_get.fn(controller, request, db_session)

        assert isinstance(result, TemplateResponse)
        assert result.template_name == "oauth/authorize.html"
        assert session["oauth_authorize"]["client_id"] == "abc"


class TestAuthorizePost:
    @pytest.mark.asyncio
    async def test_deny_redirects_with_error(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.session = {
            "user_id": "user-123",
            "oauth_authorize": {
                "client_id": "abc",
                "redirect_uri": "http://localhost/cb",
                "state": "xyz",
                "scope": "openid",
                "code_challenge": "",
            },
        }
        form_data = {"action": "deny"}
        request.form = AsyncMock(return_value=form_data)
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True):
            result = await OAuth2Controller.authorize_post.fn(controller, request, db_session)

        assert isinstance(result, Redirect)
        assert "error=access_denied" in result.url

    @pytest.mark.asyncio
    async def test_approve_returns_code(self):
        settings = _make_settings()
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.session = {
            "user_id": "user-123",
            "user_email": "test@test.com",
            "user_name": "Test",
            "user_picture_url": "",
            "oauth_authorize": {
                "client_id": "abc",
                "redirect_uri": "http://localhost/cb",
                "state": "xyz",
                "scope": "openid",
                "code_challenge": "",
            },
        }
        form_data = {"action": "allow"}
        request.form = AsyncMock(return_value=form_data)
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
             patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.authorize_post.fn(controller, request, db_session)

        assert isinstance(result, Redirect)
        assert "code=" in result.url
        assert "state=xyz" in result.url


class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_authorization_code_grant(self):
        settings = _make_settings()
        code = create_signed_token({
            "type": "code",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "scope": "openid",
            "code_challenge": "",
        }, SECRET, AUTH_CODE_TTL)

        client = _mock_client()
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/cb",
            "client_id": "abc",
            "client_secret": "secret",
            "code_verifier": "",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

        assert result.status_code == 200
        body = result.content
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == ACCESS_TOKEN_TTL
        assert body["scope"] == "openid"

    @pytest.mark.asyncio
    async def test_invalid_code_rejected(self):
        settings = _make_settings()
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "authorization_code",
            "code": "invalid-code",
            "redirect_uri": "http://localhost/cb",
            "client_id": "abc",
            "client_secret": "secret",
            "code_verifier": "",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_pkce_exchange_succeeds(self):
        settings = _make_settings()
        verifier, challenge = _generate_pkce()
        client = _mock_client(client_secret="")

        code = create_signed_token({
            "type": "code",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "scope": "openid",
            "code_challenge": challenge,
        }, SECRET, AUTH_CODE_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/cb",
            "client_id": "abc",
            "client_secret": "",
            "code_verifier": verifier,
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

        assert result.status_code == 200
        assert "access_token" in result.content


class TestRefreshTokenGrant:
    @pytest.mark.asyncio
    async def test_valid_refresh(self):
        settings = _make_settings()
        client = _mock_client()
        refresh = create_signed_token({
            "type": "refresh",
            "user_id": "user-123",
            "client_id": "abc",
            "scope": "openid",
        }, SECRET, REFRESH_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "abc",
            "client_secret": "secret",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=False)
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            mock_svc.revoke_token = AsyncMock()
            result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

        assert result.status_code == 200
        body = result.content
        assert "access_token" in body
        assert "refresh_token" in body
        # Verify old refresh token was revoked
        mock_svc.revoke_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoked_refresh_token_rejected(self):
        settings = _make_settings()
        refresh = create_signed_token({
            "type": "refresh",
            "user_id": "user-123",
            "client_id": "abc",
        }, SECRET, REFRESH_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "abc",
            "client_secret": "secret",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=True)
            result = await OAuth2Controller.token_exchange.fn(controller, request, db_session)

        assert result.status_code == 400


class TestUserInfo:
    @pytest.mark.asyncio
    async def test_valid_access_token_all_scopes(self):
        settings = _make_settings()
        access = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test User",
            "picture_url": "https://pic.url",
            "client_id": "abc",
            "scope": "openid profile email",
        }, SECRET, ACCESS_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {"authorization": f"Bearer {access}"}
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=False)
            result = await OAuth2Controller.userinfo.fn(controller, request, db_session)

        assert result.status_code == 200
        body = result.content
        assert body["sub"] == "user-123"
        assert body["email"] == "test@test.com"
        assert body["name"] == "Test User"
        assert body["picture"] == "https://pic.url"

    @pytest.mark.asyncio
    async def test_scope_filtering_openid_only(self):
        """With only openid scope, only sub is returned."""
        settings = _make_settings()
        access = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test User",
            "picture_url": "https://pic.url",
            "client_id": "abc",
            "scope": "openid",
        }, SECRET, ACCESS_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {"authorization": f"Bearer {access}"}
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=False)
            result = await OAuth2Controller.userinfo.fn(controller, request, db_session)

        assert result.status_code == 200
        body = result.content
        assert body["sub"] == "user-123"
        assert "email" not in body
        assert "name" not in body

    @pytest.mark.asyncio
    async def test_missing_bearer_token(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {}
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings()):
            result = await OAuth2Controller.userinfo.fn(controller, request, db_session)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_revoked_access_token(self):
        settings = _make_settings()
        access = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "client_id": "abc",
        }, SECRET, ACCESS_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {"authorization": f"Bearer {access}"}
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.is_token_revoked = AsyncMock(return_value=True)
            result = await OAuth2Controller.userinfo.fn(controller, request, db_session)
        assert result.status_code == 401


class TestRevoke:
    @pytest.mark.asyncio
    async def test_revoke_valid_token(self):
        settings = _make_settings()
        token = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "client_id": "abc",
        }, SECRET, ACCESS_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={"token": token})
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.revoke_token = AsyncMock()
            result = await OAuth2Controller.revoke.fn(controller, request, db_session)

        assert result.status_code == 200
        mock_svc.revoke_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_invalid_token_still_200(self):
        """RFC 7009: always return 200 even for invalid tokens."""
        settings = _make_settings()
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={"token": "invalid-token"})
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.revoke.fn(controller, request, db_session)
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_revoke_empty_token(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={"token": ""})
        db_session = AsyncMock()

        result = await OAuth2Controller.revoke.fn(controller, request, db_session)
        assert result.status_code == 200


class TestIntrospect:
    @pytest.mark.asyncio
    async def test_active_token(self):
        settings = _make_settings()
        token = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "client_id": "abc",
            "scope": "openid",
        }, SECRET, ACCESS_TOKEN_TTL)
        client = _mock_client()

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "token": token,
            "client_id": "abc",
            "client_secret": "secret",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            mock_svc.is_token_revoked = AsyncMock(return_value=False)
            result = await OAuth2Controller.introspect.fn(controller, request, db_session)

        assert result.status_code == 200
        body = result.content
        assert body["active"] is True
        assert body["sub"] == "user-123"
        assert body["scope"] == "openid"

    @pytest.mark.asyncio
    async def test_inactive_token(self):
        settings = _make_settings()
        client = _mock_client()

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "token": "bogus-token",
            "client_id": "abc",
            "client_secret": "secret",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            mock_svc.is_token_revoked = AsyncMock(return_value=False)
            result = await OAuth2Controller.introspect.fn(controller, request, db_session)

        assert result.status_code == 200
        assert result.content["active"] is False

    @pytest.mark.asyncio
    async def test_requires_client_auth(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "token": "some-token",
            "client_id": "",
            "client_secret": "",
        })
        db_session = AsyncMock()

        result = await OAuth2Controller.introspect.fn(controller, request, db_session)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_wrong_client_secret(self):
        settings = _make_settings()
        client = _mock_client()

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "token": "some-token",
            "client_id": "abc",
            "client_secret": "wrong-secret",
        })
        db_session = AsyncMock()

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings), \
             patch("skrift.controllers.oauth2.oauth2_service") as mock_svc:
            mock_svc.get_client_by_client_id = AsyncMock(return_value=client)
            result = await OAuth2Controller.introspect.fn(controller, request, db_session)

        assert result.status_code == 400
        assert result.content["error"] == "invalid_client"


class TestScopeRegistry:
    def test_builtin_scopes_registered(self):
        assert "openid" in SCOPE_DEFINITIONS
        assert "profile" in SCOPE_DEFINITIONS
        assert "email" in SCOPE_DEFINITIONS

    def test_openid_scope_claims(self):
        defn = SCOPE_DEFINITIONS["openid"]
        assert defn.claims == ["sub"]

    def test_profile_scope_claims(self):
        defn = SCOPE_DEFINITIONS["profile"]
        assert "name" in defn.claims
        assert "picture" in defn.claims

    def test_email_scope_claims(self):
        defn = SCOPE_DEFINITIONS["email"]
        assert "email" in defn.claims

    def test_register_custom_scope(self):
        register_scope("custom", "Custom scope", claims=["custom_claim"])
        defn = get_scope_definition("custom")
        assert defn is not None
        assert defn.name == "custom"
        assert defn.claims == ["custom_claim"]
        # Cleanup
        del SCOPE_DEFINITIONS["custom"]

    def test_get_unknown_scope(self):
        assert get_scope_definition("nonexistent") is None


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discovery_when_enabled(self):
        from skrift.controllers.sitemap import SitemapController

        settings = MagicMock()
        settings.oauth2_enabled = True

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()
        request.base_url = "http://localhost:8000/"

        with patch("skrift.config.get_settings", return_value=settings):
            result = await SitemapController.openid_configuration.fn(controller, request)

        assert result.status_code == 200
        body = result.content
        assert body["issuer"] == "http://localhost:8000"
        assert "/oauth/authorize" in body["authorization_endpoint"]
        assert "/oauth/token" in body["token_endpoint"]
        assert "/oauth/revoke" in body["revocation_endpoint"]
        assert "/oauth/introspect" in body["introspection_endpoint"]
        assert "S256" in body["code_challenge_methods_supported"]

    @pytest.mark.asyncio
    async def test_discovery_when_disabled(self):
        from litestar.exceptions import NotFoundException
        from skrift.controllers.sitemap import SitemapController

        settings = MagicMock()
        settings.oauth2_enabled = False

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()

        with patch("skrift.config.get_settings", return_value=settings), \
             pytest.raises(NotFoundException):
            await SitemapController.openid_configuration.fn(controller, request)
