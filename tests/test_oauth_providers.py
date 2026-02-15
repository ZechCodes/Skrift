"""Tests for OAuth provider strategy classes."""

import pytest

from skrift.auth.providers import (
    DiscordProvider,
    FacebookProvider,
    GenericProvider,
    GitHubProvider,
    GoogleProvider,
    MicrosoftProvider,
    NormalizedUserData,
    TwitterProvider,
    get_oauth_provider,
)
from skrift.setup.providers import get_provider_info


class TestGoogleProvider:
    def setup_method(self):
        self.provider = GoogleProvider("google", get_provider_info("google"))

    def test_extract_user_data(self):
        user_info = {"id": "123", "email": "test@gmail.com", "name": "Test User", "picture": "https://photo.url"}
        result = self.provider.extract_user_data(user_info)
        assert result == NormalizedUserData(oauth_id="123", email="test@gmail.com", name="Test User", picture_url="https://photo.url")

    def test_build_auth_params_includes_access_type_and_prompt(self):
        params = self.provider.build_auth_params("cid", "https://redir", ["openid"], "state123")
        assert params["access_type"] == "offline"
        assert params["prompt"] == "select_account"

    def test_requires_pkce_false(self):
        assert self.provider.requires_pkce is False


class TestGitHubProvider:
    def setup_method(self):
        self.provider = GitHubProvider("github", get_provider_info("github"))

    def test_extract_user_data(self):
        user_info = {"id": 456, "email": "dev@github.com", "name": "Dev User", "avatar_url": "https://avatar.url"}
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "456"
        assert result.email == "dev@github.com"
        assert result.name == "Dev User"
        assert result.picture_url == "https://avatar.url"

    def test_extract_user_data_falls_back_to_login(self):
        user_info = {"id": 789, "email": None, "name": None, "login": "ghuser"}
        result = self.provider.extract_user_data(user_info)
        assert result.name == "ghuser"


class TestMicrosoftProvider:
    def setup_method(self):
        self.provider = MicrosoftProvider("microsoft", get_provider_info("microsoft"))

    def test_extract_user_data(self):
        user_info = {"id": "ms-123", "mail": "user@outlook.com", "displayName": "MS User"}
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "ms-123"
        assert result.email == "user@outlook.com"
        assert result.name == "MS User"
        assert result.picture_url is None

    def test_extract_user_data_falls_back_to_upn(self):
        user_info = {"id": "ms-456", "userPrincipalName": "user@tenant.com", "displayName": "User"}
        result = self.provider.extract_user_data(user_info)
        assert result.email == "user@tenant.com"


class TestDiscordProvider:
    def setup_method(self):
        self.provider = DiscordProvider("discord", get_provider_info("discord"))

    def test_extract_user_data_with_avatar(self):
        user_info = {"id": "111", "email": "user@discord.com", "global_name": "Cool User", "avatar": "abc123"}
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "111"
        assert result.picture_url == "https://cdn.discordapp.com/avatars/111/abc123.png"

    def test_extract_user_data_no_avatar(self):
        user_info = {"id": "222", "email": "user@discord.com", "username": "discorduser", "avatar": None}
        result = self.provider.extract_user_data(user_info)
        assert result.picture_url is None
        assert result.name == "discorduser"

    def test_build_auth_params_includes_prompt(self):
        params = self.provider.build_auth_params("cid", "https://redir", ["identify"], "state")
        assert params["prompt"] == "consent"


class TestFacebookProvider:
    def setup_method(self):
        self.provider = FacebookProvider("facebook", get_provider_info("facebook"))

    def test_extract_user_data(self):
        user_info = {
            "id": "fb-123",
            "email": "user@fb.com",
            "name": "FB User",
            "picture": {"data": {"url": "https://pic.url", "is_silhouette": False}},
        }
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "fb-123"
        assert result.picture_url == "https://pic.url"

    def test_extract_user_data_silhouette_picture(self):
        user_info = {
            "id": "fb-456",
            "email": "user@fb.com",
            "name": "FB User",
            "picture": {"data": {"url": "https://default.url", "is_silhouette": True}},
        }
        result = self.provider.extract_user_data(user_info)
        assert result.picture_url is None


class TestTwitterProvider:
    def setup_method(self):
        self.provider = TwitterProvider("twitter", get_provider_info("twitter"))

    def test_requires_pkce_true(self):
        assert self.provider.requires_pkce is True

    def test_build_auth_params_with_code_challenge(self):
        params = self.provider.build_auth_params("cid", "https://redir", ["users.read"], "state", code_challenge="challenge123")
        assert params["code_challenge"] == "challenge123"
        assert params["code_challenge_method"] == "S256"

    def test_build_auth_params_without_code_challenge(self):
        params = self.provider.build_auth_params("cid", "https://redir", ["users.read"], "state")
        assert "code_challenge" not in params

    def test_build_token_data_with_verifier(self):
        data = self.provider.build_token_data("cid", "secret", "code123", "https://redir", code_verifier="verifier123")
        assert data["code_verifier"] == "verifier123"

    def test_build_token_headers_uses_basic_auth(self):
        headers = self.provider.build_token_headers("my_client", "my_secret")
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

    def test_extract_user_data(self):
        user_info = {"id": "tw-123", "name": "Twitter User", "username": "tweeter", "email": None}
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "tw-123"
        assert result.name == "Twitter User"
        assert result.picture_url is None


class TestGenericProvider:
    def setup_method(self):
        self.provider = GenericProvider("custom", get_provider_info("google"))

    def test_extract_user_data_with_id(self):
        user_info = {"id": "gen-123", "email": "user@custom.com", "name": "Custom User", "picture": "https://pic.url"}
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "gen-123"

    def test_extract_user_data_with_sub(self):
        user_info = {"sub": "sub-456", "email": "user@oidc.com", "name": "OIDC User"}
        result = self.provider.extract_user_data(user_info)
        assert result.oauth_id == "sub-456"


class TestGetOAuthProvider:
    def test_returns_google_provider(self):
        assert isinstance(get_oauth_provider("google"), GoogleProvider)

    def test_returns_github_provider(self):
        assert isinstance(get_oauth_provider("github"), GitHubProvider)

    def test_returns_microsoft_provider(self):
        assert isinstance(get_oauth_provider("microsoft"), MicrosoftProvider)

    def test_returns_discord_provider(self):
        assert isinstance(get_oauth_provider("discord"), DiscordProvider)

    def test_returns_facebook_provider(self):
        assert isinstance(get_oauth_provider("facebook"), FacebookProvider)

    def test_returns_twitter_provider(self):
        assert isinstance(get_oauth_provider("twitter"), TwitterProvider)

    def test_returns_generic_for_dummy(self):
        provider = get_oauth_provider("dummy")
        assert isinstance(provider, GenericProvider)

    def test_raises_for_unknown(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            get_oauth_provider("nonexistent")


class TestResolveUrl:
    def test_resolves_tenant_placeholder(self):
        provider = get_oauth_provider("microsoft")
        url = provider.resolve_url("https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize", "my-tenant")
        assert url == "https://login.microsoftonline.com/my-tenant/oauth2/v2.0/authorize"

    def test_defaults_to_common(self):
        provider = get_oauth_provider("microsoft")
        url = provider.resolve_url("https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize")
        assert "common" in url

    def test_no_placeholder_unchanged(self):
        provider = get_oauth_provider("google")
        url = provider.resolve_url("https://accounts.google.com/o/oauth2/v2/auth")
        assert url == "https://accounts.google.com/o/oauth2/v2/auth"
