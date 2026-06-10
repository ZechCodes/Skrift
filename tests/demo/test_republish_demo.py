"""Tests for the republish demo project."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import skrift.config as config_mod
from skrift.config import clear_settings_cache, set_config_path


DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo" / "republish-demo"


@pytest.fixture(autouse=True)
def demo_path():
    if str(DEMO_ROOT) not in sys.path:
        sys.path.insert(0, str(DEMO_ROOT))
    yield
    config_mod._config_path_override = None
    clear_settings_cache()


def test_target_config_enables_republish(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("TARGET_DATABASE_URL", "sqlite+aiosqlite:///target.db")
    set_config_path(DEMO_ROOT / "target.app.yaml")

    settings = config_mod.get_settings()

    assert settings.api_grants.enabled is True
    assert settings.api_grants.discovery_enabled is True
    assert settings.republish.enabled is True
    assert settings.republish.default_page_type == "post"
    assert settings.republish.default_delete_behavior == "unpublish"


def test_source_demo_payload_matches_source_origin():
    source = importlib.import_module("republishdemo.source")

    payload = source._demo_payload("http://localhost:8093")

    assert payload["canonical_url"] == "http://localhost:8093/posts/republish-demo"
    assert payload["title"] == "Republish Demo Post"
    assert payload["tags"] == ["demo", "republish"]


def test_source_demo_pkce_pair_is_s256_shape():
    source = importlib.import_module("republishdemo.source")

    verifier, challenge = source._pkce_pair()

    assert verifier
    assert challenge
    assert "=" not in challenge
