"""Tests for SiteConfig and multi-site settings parsing."""

from unittest.mock import patch

import pytest

from skrift.config import SiteConfig, PageTypeConfig, Settings


class TestSiteConfig:
    def test_minimal(self):
        config = SiteConfig(subdomain="blog")
        assert config.subdomain == "blog"
        assert config.controllers == []
        assert config.theme == ""
        assert config.page_types == []

    def test_full(self):
        config = SiteConfig(
            subdomain="docs",
            controllers=["myapp.controllers:DocsController"],
            theme="docs-theme",
            page_types=[{"name": "article", "plural": "articles", "icon": "book"}],
        )
        assert config.subdomain == "docs"
        assert config.controllers == ["myapp.controllers:DocsController"]
        assert config.theme == "docs-theme"
        assert len(config.page_types) == 1
        assert config.page_types[0].name == "article"


class TestSettingsDomainAndSites:
    def test_defaults(self):
        settings = Settings(secret_key="test")
        assert settings.domain == ""
        assert settings.sites == {}

    def test_domain_set(self):
        settings = Settings(secret_key="test", domain="example.com")
        assert settings.domain == "example.com"

    def test_sites_set(self):
        settings = Settings(
            secret_key="test",
            domain="example.com",
            sites={
                "blog": SiteConfig(subdomain="blog"),
                "docs": SiteConfig(subdomain="docs", theme="docs-theme"),
            },
        )
        assert len(settings.sites) == 2
        assert settings.sites["blog"].subdomain == "blog"
        assert settings.sites["docs"].theme == "docs-theme"


class TestGetSettingsParsing:
    def test_parses_domain_and_sites(self, tmp_path, monkeypatch):
        """get_settings() parses domain and sites from app.yaml."""
        import yaml
        from skrift.config import get_settings, clear_settings_cache

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = {
            "domain": "example.com",
            "sites": {
                "blog": {
                    "subdomain": "blog",
                    "controllers": ["blog:BlogController"],
                    "theme": "blog-theme",
                },
            },
        }
        config_path = tmp_path / "app.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f)

        clear_settings_cache()
        with patch("skrift.config.get_config_path", return_value=config_path):
            settings = get_settings()

        clear_settings_cache()

        assert settings.domain == "example.com"
        assert "blog" in settings.sites
        assert settings.sites["blog"].subdomain == "blog"
        assert settings.sites["blog"].controllers == ["blog:BlogController"]
        assert settings.sites["blog"].theme == "blog-theme"

    def test_no_sites_key_is_empty(self, tmp_path, monkeypatch):
        """get_settings() returns empty sites when key is absent."""
        import yaml
        from skrift.config import get_settings, clear_settings_cache

        monkeypatch.setenv("SECRET_KEY", "test-secret")

        config = {}
        config_path = tmp_path / "app.yaml"
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f)

        clear_settings_cache()
        with patch("skrift.config.get_config_path", return_value=config_path):
            settings = get_settings()

        clear_settings_cache()

        assert settings.domain == ""
        assert settings.sites == {}
