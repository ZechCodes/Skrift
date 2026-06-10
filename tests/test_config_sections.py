"""Tests for the config section registry (register_config_section)."""

import os
from unittest.mock import patch

import pytest
import yaml
from pydantic import BaseModel

import skrift.config as config_mod
from skrift.config import (
    clear_settings_cache,
    get_settings,
    register_config_section,
    set_config_path,
)


class ShopConfig(BaseModel):
    enabled: bool = False
    currency: str = "USD"


@pytest.fixture
def clean_registry(monkeypatch):
    """Remove test-registered sections and config overrides afterwards."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    before = dict(config_mod._CONFIG_SECTIONS)
    yield
    config_mod._CONFIG_SECTIONS.clear()
    config_mod._CONFIG_SECTIONS.update(before)
    config_mod._config_path_override = None
    clear_settings_cache()


def _write_config(tmp_path, data: dict):
    config_file = tmp_path / "app.yaml"
    config_file.write_text(yaml.safe_dump(data))
    set_config_path(config_file)


class TestRegisterConfigSection:
    def test_extension_section_parsed_from_yaml(self, tmp_path, clean_registry):
        register_config_section("shop", ShopConfig)
        _write_config(
            tmp_path,
            {"secret_key": "x", "shop": {"enabled": True, "currency": "EUR"}},
        )

        settings = get_settings()
        assert isinstance(settings.shop, ShopConfig)
        assert settings.shop.enabled is True
        assert settings.shop.currency == "EUR"

    def test_extension_section_defaults_when_absent(self, tmp_path, clean_registry):
        register_config_section("shop", ShopConfig)
        _write_config(tmp_path, {"secret_key": "x"})

        settings = get_settings()
        assert isinstance(settings.shop, ShopConfig)
        assert settings.shop.enabled is False

    def test_extension_section_defaults_without_config_file(self, clean_registry):
        register_config_section("shop", ShopConfig)
        set_config_path(config_mod.Path("/nonexistent/app.yaml"))
        with patch.dict(os.environ, {"SECRET_KEY": "x"}, clear=False):
            settings = get_settings()
        assert isinstance(settings.shop, ShopConfig)

    def test_reserved_name_rejected(self, clean_registry):
        with pytest.raises(ValueError, match="reserved"):
            register_config_section("storage", ShopConfig)

    def test_invalid_identifier_rejected(self, clean_registry):
        with pytest.raises(ValueError, match="identifier"):
            register_config_section("my-shop", ShopConfig)

    def test_conflicting_reregistration_rejected(self, clean_registry):
        register_config_section("shop", ShopConfig)

        class OtherConfig(BaseModel):
            pass

        with pytest.raises(ValueError, match="already registered"):
            register_config_section("shop", OtherConfig)

    def test_idempotent_reregistration_allowed(self, clean_registry):
        register_config_section("shop", ShopConfig)
        register_config_section("shop", ShopConfig)

    def test_registration_clears_settings_cache(self, tmp_path, clean_registry):
        _write_config(tmp_path, {"secret_key": "x"})
        first = get_settings()
        register_config_section("shop", ShopConfig)
        assert get_settings() is not first


class TestBuiltinSections:
    def test_api_keys_section_parsed_from_yaml(self, tmp_path, clean_registry):
        """api_keys was documented but previously never parsed from app.yaml."""
        _write_config(
            tmp_path,
            {"secret_key": "x", "api_keys": {"max_keys_per_user": 3}},
        )

        settings = get_settings()
        assert settings.api_keys.max_keys_per_user == 3

    def test_set_config_path_clears_cache(self, tmp_path, clean_registry):
        _write_config(tmp_path, {"secret_key": "x", "debug": True})
        assert get_settings().debug is True

        other = tmp_path / "app2.yaml"
        other.write_text(yaml.safe_dump({"secret_key": "x", "debug": False}))
        set_config_path(other)
        assert get_settings().debug is False
