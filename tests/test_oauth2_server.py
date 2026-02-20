"""Tests for the OAuth2 Authorization Server controller."""

import base64
import hashlib
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from litestar.response import Redirect, Template as TemplateResponse

from skrift.auth.tokens import create_signed_token
from skrift.controllers.oauth2 import (
    OAuth2Controller,
    _find_client,
    _verify_pkce,
    _json_error,
    AUTH_CODE_TTL,
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
)


def _make_settings(clients=None):
    """Create a mock settings object with OAuth2 clients."""
    settings = MagicMock()
    settings.secret_key = "test-secret-key"
    if clients is None:
        clients = []
    mock_clients = []
    for c in clients:
        mc = MagicMock()
        mc.client_id = c["client_id"]
        mc.client_secret = c.get("client_secret", "")
        mc.redirect_uris = c.get("redirect_uris", [])
        mock_clients.append(mc)
    settings.oauth2.clients = mock_clients
    return settings


def _generate_pkce():
    """Generate a PKCE code_verifier and code_challenge pair."""
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


class TestFindClient:
    def test_finds_existing_client(self):
        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])):
            client = _find_client("abc")
            assert client is not None
            assert client.client_id == "abc"

    def test_returns_none_for_unknown(self):
        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([])):
            assert _find_client("unknown") is None


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


class TestAuthorizeGet:
    @pytest.mark.asyncio
    async def test_requires_code_response_type(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.query_params = {"response_type": "token", "client_id": "abc"}
        request.session = {}

        result = await OAuth2Controller.authorize_get.fn(controller, request)
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

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([])):
            result = await OAuth2Controller.authorize_get.fn(controller, request)
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

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([
            {"client_id": "abc", "redirect_uris": ["http://localhost/cb"]},
        ])):
            result = await OAuth2Controller.authorize_get.fn(controller, request)
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

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([
            {"client_id": "abc", "client_secret": "", "redirect_uris": ["http://localhost/cb"]},
        ])):
            result = await OAuth2Controller.authorize_get.fn(controller, request)
        assert result.status_code == 400

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

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([
            {"client_id": "abc", "redirect_uris": ["http://localhost/cb"]},
        ])):
            result = await OAuth2Controller.authorize_get.fn(controller, request)

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

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings([
            {"client_id": "abc", "redirect_uris": ["http://localhost/cb"]},
        ])):
            result = await OAuth2Controller.authorize_get.fn(controller, request)

        assert isinstance(result, TemplateResponse)
        assert result.template_name == "oauth/authorize.html"
        assert session["oauth_authorize"]["client_id"] == "abc"


class TestAuthorizePost:
    @pytest.mark.asyncio
    async def test_deny_redirects_with_error(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.session = {
            "user_id": "user-123",
            "oauth_authorize": {
                "client_id": "abc",
                "redirect_uri": "http://localhost/cb",
                "state": "xyz",
                "code_challenge": "",
            },
        }

        form_data = {"action": "deny"}
        request.form = AsyncMock(return_value=form_data)

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
             patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.authorize_post.fn(controller, request)

        assert isinstance(result, Redirect)
        assert "error=access_denied" in result.url

    @pytest.mark.asyncio
    async def test_approve_returns_code(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])
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
                "code_challenge": "",
            },
        }

        form_data = {"action": "allow"}
        request.form = AsyncMock(return_value=form_data)

        with patch("skrift.controllers.oauth2.verify_csrf", new_callable=AsyncMock, return_value=True), \
             patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.authorize_post.fn(controller, request)

        assert isinstance(result, Redirect)
        assert "code=" in result.url
        assert "state=xyz" in result.url


class TestTokenExchange:
    @pytest.mark.asyncio
    async def test_authorization_code_grant(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])
        code = create_signed_token({
            "type": "code",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "code_challenge": "",
        }, settings.secret_key, AUTH_CODE_TTL)

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

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)

        assert result.status_code == 200
        body = result.content
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == ACCESS_TOKEN_TTL

    @pytest.mark.asyncio
    async def test_invalid_code_rejected(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])

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

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)

        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_pkce_required_for_public_client(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "", "redirect_uris": ["http://localhost/cb"]},
        ])
        _, challenge = _generate_pkce()

        code = create_signed_token({
            "type": "code",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "code_challenge": challenge,
        }, settings.secret_key, AUTH_CODE_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()

        # Missing code_verifier
        request.form = AsyncMock(return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/cb",
            "client_id": "abc",
            "client_secret": "",
            "code_verifier": "",
        })

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_pkce_exchange_succeeds(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "", "redirect_uris": ["http://localhost/cb"]},
        ])
        verifier, challenge = _generate_pkce()

        code = create_signed_token({
            "type": "code",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "code_challenge": challenge,
        }, settings.secret_key, AUTH_CODE_TTL)

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

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)

        assert result.status_code == 200
        body = result.content
        assert "access_token" in body
        assert "refresh_token" in body

    @pytest.mark.asyncio
    async def test_client_id_mismatch_rejected(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])
        code = create_signed_token({
            "type": "code",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test",
            "picture_url": "",
            "client_id": "abc",
            "redirect_uri": "http://localhost/cb",
            "code_challenge": "",
        }, settings.secret_key, AUTH_CODE_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost/cb",
            "client_id": "different-client",
            "client_secret": "secret",
            "code_verifier": "",
        })

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)
        assert result.status_code == 400


class TestRefreshTokenGrant:
    @pytest.mark.asyncio
    async def test_valid_refresh(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])
        refresh = create_signed_token({
            "type": "refresh",
            "user_id": "user-123",
            "client_id": "abc",
        }, settings.secret_key, REFRESH_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": "abc",
            "client_secret": "secret",
        })

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)

        assert result.status_code == 200
        body = result.content
        assert "access_token" in body
        assert "refresh_token" in body
        # New refresh token should be a valid token
        from skrift.auth.tokens import verify_signed_token
        new_payload = verify_signed_token(body["refresh_token"], settings.secret_key)
        assert new_payload is not None
        assert new_payload["type"] == "refresh"
        assert new_payload["user_id"] == "user-123"

    @pytest.mark.asyncio
    async def test_invalid_refresh_token_rejected(self):
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "refresh_token",
            "refresh_token": "bogus-token",
            "client_id": "abc",
            "client_secret": "secret",
        })

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_access_token_rejected_as_refresh(self):
        """Type field prevents using access token as refresh token."""
        settings = _make_settings([
            {"client_id": "abc", "client_secret": "secret", "redirect_uris": ["http://localhost/cb"]},
        ])
        access = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "client_id": "abc",
        }, settings.secret_key, ACCESS_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.form = AsyncMock(return_value={
            "grant_type": "refresh_token",
            "refresh_token": access,  # Wrong type!
            "client_id": "abc",
            "client_secret": "secret",
        })

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.token_exchange.fn(controller, request)
        assert result.status_code == 400


class TestUserInfo:
    @pytest.mark.asyncio
    async def test_valid_access_token(self):
        settings = _make_settings()
        access = create_signed_token({
            "type": "access",
            "user_id": "user-123",
            "email": "test@test.com",
            "name": "Test User",
            "picture_url": "https://pic.url",
            "client_id": "abc",
        }, settings.secret_key, ACCESS_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {"authorization": f"Bearer {access}"}

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.userinfo.fn(controller, request)

        assert result.status_code == 200
        body = result.content
        assert body["sub"] == "user-123"
        assert body["email"] == "test@test.com"
        assert body["name"] == "Test User"
        assert body["picture"] == "https://pic.url"

    @pytest.mark.asyncio
    async def test_missing_bearer_token(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {}

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings()):
            result = await OAuth2Controller.userinfo.fn(controller, request)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token(self):
        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {"authorization": "Bearer invalid-token"}

        with patch("skrift.controllers.oauth2.get_settings", return_value=_make_settings()):
            result = await OAuth2Controller.userinfo.fn(controller, request)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_token_rejected_as_access(self):
        """Type field prevents using refresh token at userinfo endpoint."""
        settings = _make_settings()
        refresh = create_signed_token({
            "type": "refresh",
            "user_id": "user-123",
            "client_id": "abc",
        }, settings.secret_key, REFRESH_TOKEN_TTL)

        controller = OAuth2Controller(owner=MagicMock())
        request = MagicMock()
        request.headers = {"authorization": f"Bearer {refresh}"}

        with patch("skrift.controllers.oauth2.get_settings", return_value=settings):
            result = await OAuth2Controller.userinfo.fn(controller, request)
        assert result.status_code == 401
