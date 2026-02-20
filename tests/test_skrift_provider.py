"""Tests for the SkriftProvider class."""

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from skrift.auth.providers import SkriftProvider, NormalizedUserData
from skrift.setup.providers import OAUTH_PROVIDERS


def _make_provider():
    """Create a SkriftProvider with mock provider info."""
    provider_info = OAUTH_PROVIDERS["skrift"]
    return SkriftProvider("skrift", provider_info)


def _make_settings(server_url="https://hub.example.com"):
    """Create mock settings with a skrift provider config."""
    settings = MagicMock()
    provider_config = MagicMock()
    provider_config.server_url = server_url
    settings.auth.providers = {"skrift": provider_config}
    return settings


class TestSkriftProviderRequiresPkce:
    def test_always_requires_pkce(self):
        provider = _make_provider()
        assert provider.requires_pkce is True


class TestResolveUrl:
    def test_replaces_server_url_placeholder(self):
        provider = _make_provider()
        with patch("skrift.config.get_settings", return_value=_make_settings()):
            result = provider.resolve_url("{server_url}/oauth/authorize")
        assert result == "https://hub.example.com/oauth/authorize"

    def test_strips_trailing_slash_from_server_url(self):
        provider = _make_provider()
        with patch("skrift.config.get_settings", return_value=_make_settings("https://hub.example.com/")):
            result = provider.resolve_url("{server_url}/oauth/token")
        assert result == "https://hub.example.com/oauth/token"

    def test_passthrough_without_placeholder(self):
        provider = _make_provider()
        result = provider.resolve_url("https://other.com/auth")
        assert result == "https://other.com/auth"


class TestBuildAuthParams:
    def test_includes_pkce_params(self):
        provider = _make_provider()
        params = provider.build_auth_params(
            client_id="client-abc",
            redirect_uri="https://spoke.example.com/cb",
            scopes=["openid", "profile"],
            state="state123",
            code_challenge="challenge-value",
        )
        assert params["code_challenge"] == "challenge-value"
        assert params["code_challenge_method"] == "S256"
        assert params["client_id"] == "client-abc"
        assert params["response_type"] == "code"

    def test_no_pkce_when_no_challenge(self):
        provider = _make_provider()
        params = provider.build_auth_params(
            client_id="client-abc",
            redirect_uri="https://spoke.example.com/cb",
            scopes=["openid"],
            state="state123",
        )
        assert "code_challenge" not in params


class TestBuildTokenData:
    def test_includes_code_verifier(self):
        provider = _make_provider()
        data = provider.build_token_data(
            client_id="client-abc",
            client_secret="secret",
            code="auth-code",
            redirect_uri="https://spoke.example.com/cb",
            code_verifier="verifier-value",
        )
        assert data["code_verifier"] == "verifier-value"
        assert data["client_secret"] == "secret"

    def test_public_client_omits_secret(self):
        provider = _make_provider()
        data = provider.build_token_data(
            client_id="client-abc",
            client_secret="",
            code="auth-code",
            redirect_uri="https://spoke.example.com/cb",
            code_verifier="verifier-value",
        )
        assert "client_secret" not in data
        assert data["code_verifier"] == "verifier-value"


class TestExtractUserData:
    def test_extracts_from_userinfo_response(self):
        provider = _make_provider()
        user_info = {
            "sub": "user-123",
            "email": "alice@example.com",
            "name": "Alice",
            "picture": "https://pic.url/alice.jpg",
        }
        result = provider.extract_user_data(user_info)
        assert isinstance(result, NormalizedUserData)
        assert result.oauth_id == "user-123"
        assert result.email == "alice@example.com"
        assert result.name == "Alice"
        assert result.picture_url == "https://pic.url/alice.jpg"

    def test_handles_missing_fields(self):
        provider = _make_provider()
        user_info = {"sub": "user-456"}
        result = provider.extract_user_data(user_info)
        assert result.oauth_id == "user-456"
        assert result.email is None
        assert result.name is None
        assert result.picture_url is None


class TestFetchUserInfo:
    @pytest.mark.asyncio
    async def test_resolves_server_url_before_fetch(self):
        provider = _make_provider()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"sub": "u1", "email": "a@b.com"}

        with patch("skrift.config.get_settings", return_value=_make_settings()), \
             patch("skrift.auth.providers.httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await provider.fetch_user_info("access-token-xyz")

        mock_client.get.assert_called_once()
        call_url = mock_client.get.call_args[0][0]
        assert call_url == "https://hub.example.com/oauth/userinfo"
        assert result == {"sub": "u1", "email": "a@b.com"}


class TestProviderRegistration:
    def test_skrift_in_oauth_providers(self):
        assert "skrift" in OAUTH_PROVIDERS
        info = OAUTH_PROVIDERS["skrift"]
        assert info.name == "Skrift"
        assert "{server_url}" in info.auth_url
        assert "{server_url}" in info.token_url
        assert "{server_url}" in info.userinfo_url

    def test_skrift_in_provider_classes(self):
        from skrift.auth.providers import _PROVIDER_CLASSES
        assert "skrift" in _PROVIDER_CLASSES
        assert _PROVIDER_CLASSES["skrift"] is SkriftProvider
