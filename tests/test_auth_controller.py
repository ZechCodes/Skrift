"""Tests for the authentication controller."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from skrift.controllers.auth import (
    _is_safe_redirect_url,
    _get_safe_redirect_url,
    _set_login_session,
    _exchange_and_fetch,
)


class TestIsSafeRedirectUrl:
    def test_relative_path_is_safe(self):
        assert _is_safe_redirect_url("/admin", []) is True

    def test_protocol_relative_is_unsafe(self):
        assert _is_safe_redirect_url("//evil.com", []) is False

    def test_https_allowed_domain(self):
        assert _is_safe_redirect_url("https://example.com/page", ["example.com"]) is True

    def test_http_allowed_domain(self):
        assert _is_safe_redirect_url("http://example.com/page", ["example.com"]) is True

    def test_subdomain_matches(self):
        assert _is_safe_redirect_url("https://app.example.com", ["example.com"]) is True

    def test_unknown_domain_rejected(self):
        assert _is_safe_redirect_url("https://evil.com/page", ["example.com"]) is False

    def test_wildcard_pattern(self):
        assert _is_safe_redirect_url("https://app.example.com", ["*.example.com"]) is True

    def test_javascript_scheme_rejected(self):
        assert _is_safe_redirect_url("javascript:alert(1)", ["example.com"]) is False


class TestGetSafeRedirectUrl:
    def test_returns_session_url_if_safe(self):
        request = MagicMock()
        request.session = {"auth_next": "/dashboard"}
        result = _get_safe_redirect_url(request, [])
        assert result == "/dashboard"
        assert "auth_next" not in request.session

    def test_returns_default_if_no_session(self):
        request = MagicMock()
        request.session = {}
        result = _get_safe_redirect_url(request, [])
        assert result == "/"

    def test_returns_default_if_unsafe(self):
        request = MagicMock()
        request.session = {"auth_next": "https://evil.com"}
        result = _get_safe_redirect_url(request, [])
        assert result == "/"


class TestSetLoginSession:
    def test_populates_user_data(self):
        request = MagicMock()
        session_dict = {}
        request.session = session_dict

        user = MagicMock()
        user.id = uuid4()
        user.name = "Test User"
        user.email = "test@example.com"
        user.picture_url = "https://pic.url"

        _set_login_session(request, user)

        assert session_dict["user_id"] == str(user.id)
        assert session_dict["user_name"] == "Test User"
        assert session_dict["user_email"] == "test@example.com"

    def test_preserves_flash(self):
        session_dict = {"flash": "Welcome!", "_nid": "abc123"}
        request = MagicMock()
        request.session = session_dict

        user = MagicMock()
        user.id = uuid4()
        user.name = "User"
        user.email = "u@test.com"
        user.picture_url = None

        _set_login_session(request, user)

        assert session_dict.get("flash") == "Welcome!"
        assert session_dict.get("_nid") == "abc123"


class TestExchangeAndFetch:
    @pytest.mark.asyncio
    async def test_successful_exchange(self):
        """Test successful OAuth code exchange and user info fetch."""
        from skrift.auth.providers import NormalizedUserData

        mock_provider = MagicMock()
        mock_provider.resolve_url.return_value = "https://token.url"
        mock_provider.build_token_data.return_value = {"code": "abc"}
        mock_provider.build_token_headers.return_value = {"Accept": "application/json"}
        mock_provider.provider_info.token_url = "https://token.url"
        mock_provider.extract_user_data.return_value = NormalizedUserData(
            oauth_id="123", email="test@test.com", name="Test", picture_url=None
        )
        mock_provider.fetch_user_info = AsyncMock(return_value={"id": "123"})

        mock_settings = MagicMock()
        mock_settings.auth.providers = {
            "google": MagicMock(client_id="cid", client_secret="secret", tenant_id=None)
        }

        # Mock httpx response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token123"}

        with patch("skrift.controllers.auth.get_oauth_provider", return_value=mock_provider), \
             patch("skrift.controllers.auth.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            user_data, user_info = await _exchange_and_fetch(
                "google", mock_settings, "code123", "https://redirect"
            )

            assert user_data.oauth_id == "123"
            assert user_info == {"id": "123"}

    @pytest.mark.asyncio
    async def test_exchange_with_explicit_credentials(self):
        """Test exchange with explicit client_id/secret (setup mode)."""
        from skrift.auth.providers import NormalizedUserData

        mock_provider = MagicMock()
        mock_provider.resolve_url.return_value = "https://token.url"
        mock_provider.build_token_data.return_value = {"code": "abc"}
        mock_provider.build_token_headers.return_value = {}
        mock_provider.provider_info.token_url = "https://token.url"
        mock_provider.extract_user_data.return_value = NormalizedUserData(
            oauth_id="456", email="setup@test.com", name="Admin", picture_url=None
        )
        mock_provider.fetch_user_info = AsyncMock(return_value={"id": "456"})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token456"}

        with patch("skrift.controllers.auth.get_oauth_provider", return_value=mock_provider), \
             patch("skrift.controllers.auth.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            user_data, user_info = await _exchange_and_fetch(
                "github", None, "code456", "https://redirect",
                client_id="explicit-id", client_secret="explicit-secret",
            )

            assert user_data.oauth_id == "456"


class TestOAuthLogin:
    @pytest.mark.asyncio
    async def test_unknown_provider_404(self):
        """Unknown provider raises 404."""
        from litestar.exceptions import NotFoundException
        from skrift.controllers.auth import AuthController

        auth = AuthController(owner=MagicMock())
        request = MagicMock()
        request.session = {}

        with patch("skrift.controllers.auth.get_settings") as mock_settings, \
             patch("skrift.controllers.auth.get_provider_info", return_value=None):
            mock_settings.return_value.auth.allowed_redirect_domains = []

            with pytest.raises(NotFoundException):
                await AuthController.oauth_login.fn(auth, request, "nonexistent")


class TestLogout:
    @pytest.mark.asyncio
    async def test_clears_session(self):
        from skrift.controllers.auth import AuthController

        auth = AuthController(owner=MagicMock())
        session = {"user_id": "123", "user_name": "test"}
        request = MagicMock()
        request.session = session

        result = await AuthController.logout.fn(auth, request)
        assert len(session) == 0
