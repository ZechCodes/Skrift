"""Tests for the primary-auth method groundwork."""

from unittest.mock import MagicMock, patch

from skrift.config import AuthConfig


class TestAuthConfigPrimaryMethodTypes:
    def test_dummy_provider_maps_to_dummy_method(self):
        config = AuthConfig(providers={"dummy": {}})
        assert config.get_primary_auth_method_type("dummy") == "dummy"

    def test_oauth_provider_maps_to_oauth_method(self):
        config = AuthConfig(
            providers={"google": {"client_id": "id", "client_secret": "secret"}}
        )
        assert config.get_primary_auth_method_type("google") == "oauth"

    def test_methods_config_populates_method_and_provider_views(self):
        config = AuthConfig(
            methods={
                "github-work": {
                    "type": "oauth",
                    "provider": "github",
                    "client_id": "id",
                    "client_secret": "secret",
                }
            }
        )

        assert config.get_method_keys() == ["github-work"]
        assert config.get_method_config("github-work")["provider"] == "github"
        assert config.get_provider_type("github-work") == "github"
        assert config.get_primary_auth_method_type("github-work") == "oauth"
        assert config.providers["github-work"].client_id == "id"

    def test_get_method_keys_falls_back_to_providers(self):
        config = AuthConfig(
            providers={"google": {"client_id": "id", "client_secret": "secret"}}
        )
        assert config.get_method_keys() == ["google"]

    def test_passkey_method_config_keeps_passkey_type(self):
        config = AuthConfig(
            methods={
                "passkey": {
                    "type": "passkey",
                    "factor_key": "passkey",
                    "label": "Passkey",
                }
            }
        )

        assert config.get_method_keys() == ["passkey"]
        assert config.get_primary_auth_method_type("passkey") == "passkey"
        assert config.get_method_config("passkey")["factor_key"] == "passkey"


class TestPrimaryAuthMethodRegistry:
    def test_get_primary_auth_method_returns_oauth_method_for_oauth_provider(self):
        from skrift.auth.methods.oauth import OAuthPrimaryAuthMethod
        from skrift.auth.methods.registry import get_primary_auth_method

        settings = MagicMock()
        settings.auth.get_primary_auth_method_type.return_value = "oauth"

        with patch("skrift.auth.methods.registry.get_settings", return_value=settings):
            method = get_primary_auth_method("google")

        assert isinstance(method, OAuthPrimaryAuthMethod)
        assert method.method_key == "google"

    def test_get_primary_auth_method_returns_dummy_method_for_dummy_provider(self):
        from skrift.auth.methods.dummy import DummyPrimaryAuthMethod
        from skrift.auth.methods.registry import get_primary_auth_method

        settings = MagicMock()
        settings.auth.get_primary_auth_method_type.return_value = "dummy"

        with patch("skrift.auth.methods.registry.get_settings", return_value=settings):
            method = get_primary_auth_method("dummy")

        assert isinstance(method, DummyPrimaryAuthMethod)
        assert method.method_key == "dummy"

    def test_get_primary_auth_method_returns_passkey_method_for_passkey_config(self):
        from skrift.auth.methods.passkey import PasskeyPrimaryAuthMethod
        from skrift.auth.methods.registry import get_primary_auth_method

        settings = MagicMock()
        settings.auth.get_primary_auth_method_type.return_value = "passkey"

        with patch("skrift.auth.methods.registry.get_settings", return_value=settings):
            method = get_primary_auth_method("passkey")

        assert isinstance(method, PasskeyPrimaryAuthMethod)
        assert method.method_key == "passkey"


class TestGetSettingsWithMethodsConfig:
    def test_get_settings_parses_auth_methods(self, tmp_path, monkeypatch):
        import yaml
        from skrift.config import clear_settings_cache, get_settings

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = {
            "auth": {
                "methods": {
                    "google": {
                        "type": "oauth",
                        "client_id": "id",
                        "client_secret": "secret",
                    },
                    "dummy": {"type": "dummy"},
                }
            }
        }
        config_path = tmp_path / "app.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f)

        clear_settings_cache()
        with patch("skrift.config.get_config_path", return_value=config_path):
            settings = get_settings()
        clear_settings_cache()

        assert set(settings.auth.get_method_keys()) == {"google", "dummy"}
        assert settings.auth.get_primary_auth_method_type("google") == "oauth"
        assert settings.auth.get_primary_auth_method_type("dummy") == "dummy"
        assert settings.auth.providers["google"].client_id == "id"
