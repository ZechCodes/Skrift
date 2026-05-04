"""Edge cases in BotDetectionController.

Covers paths the happy-path tests skip:

- Pixel/CSS/verify when bot detection store is missing from app state
- Verify endpoint with malformed JSON keys
- Verify endpoint when js_challenge is disabled
- Pixel endpoint when pixel_beacon is disabled
- Tokens with empty strings
"""

from __future__ import annotations

import asyncio

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from skrift.bot_detection.beacon import (
    PIXEL_LOADED_NS,
    make_pixel_token,
)
from skrift.bot_detection.challenge import (
    JS_CHALLENGE_NS,
    make_challenge_token,
)
from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.controllers import BotDetectionController
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.lib.hooks import hooks


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


def _build_app(monkeypatch, *, store, config: BotDetectionConfig):
    from skrift.config import Settings

    settings = Settings(secret_key="edge-secret", bot_detection=config)
    monkeypatch.setattr("skrift.config.get_settings", lambda: settings)
    app = Litestar(route_handlers=[BotDetectionController])
    if store is not None:
        app.state.bot_detection_store = store
    return app, settings


class TestPixelEdgeCases:
    def test_pixel_returns_gif_when_store_missing(self, monkeypatch):
        """No store on app.state -> still serves the GIF, just doesn't record."""
        app, settings = _build_app(
            monkeypatch, store=None, config=BotDetectionConfig(enabled=True)
        )
        token, sig = make_pixel_token(settings.secret_key)
        with TestClient(app) as client:
            r = client.get(f"/_bot/p.gif?t={token}&s={sig}")
        assert r.status_code == 200
        assert r.content[:3] == b"GIF"

    def test_pixel_does_nothing_when_metric_disabled(self, monkeypatch):
        """Pixel beacon disabled -> token validation bypassed, no record."""
        store = InMemoryBotStateStore()
        config = BotDetectionConfig(
            enabled=True, pixel_beacon={"enabled": False}
        )
        app, settings = _build_app(monkeypatch, store=store, config=config)
        token, sig = make_pixel_token(settings.secret_key)
        with TestClient(app) as client:
            r = client.get(f"/_bot/p.gif?t={token}&s={sig}")
        assert r.status_code == 200
        assert asyncio.run(store.get(PIXEL_LOADED_NS, "testclient")) is None

    def test_pixel_with_no_token_does_not_record(self, monkeypatch):
        store = InMemoryBotStateStore()
        app, _ = _build_app(
            monkeypatch, store=store, config=BotDetectionConfig(enabled=True)
        )
        with TestClient(app) as client:
            r = client.get("/_bot/p.gif")  # no t / s params
        assert r.status_code == 200
        assert asyncio.run(store.get(PIXEL_LOADED_NS, "testclient")) is None

    def test_pixel_with_token_but_no_signature_does_not_record(self, monkeypatch):
        store = InMemoryBotStateStore()
        app, _ = _build_app(
            monkeypatch, store=store, config=BotDetectionConfig(enabled=True)
        )
        with TestClient(app) as client:
            r = client.get("/_bot/p.gif?t=abcd")
        assert r.status_code == 200
        assert asyncio.run(store.get(PIXEL_LOADED_NS, "testclient")) is None


class TestVerifyEdgeCases:
    def test_verify_silent_when_js_challenge_disabled(self, monkeypatch):
        """Even with valid token, verify is a no-op when js_challenge is off."""
        store = InMemoryBotStateStore()
        config = BotDetectionConfig(
            enabled=True, js_challenge={"enabled": False}
        )
        app, settings = _build_app(monkeypatch, store=store, config=config)
        token, sig = make_challenge_token(settings.secret_key)
        with TestClient(app) as client:
            r = client.post(
                "/_bot/verify",
                json={
                    "token": token,
                    "signature": sig,
                    "webdriver": False,
                    "plugins": 1,
                    "languages": 1,
                    "chrome": True,
                    "canvas": 200,
                },
            )
        assert r.status_code == 204
        assert asyncio.run(store.get(JS_CHALLENGE_NS, "testclient")) is None

    def test_verify_returns_204_when_store_missing(self, monkeypatch):
        config = BotDetectionConfig(
            enabled=True, js_challenge={"enabled": True}
        )
        app, settings = _build_app(monkeypatch, store=None, config=config)
        token, sig = make_challenge_token(settings.secret_key)
        with TestClient(app) as client:
            r = client.post(
                "/_bot/verify",
                json={
                    "token": token,
                    "signature": sig,
                    "webdriver": False,
                    "plugins": 1,
                    "languages": 1,
                    "chrome": True,
                    "canvas": 200,
                },
            )
        assert r.status_code == 204

    def test_verify_with_missing_keys_does_not_crash(self, monkeypatch):
        """Half-formed payload — evaluator coerces missing fields, returns fail."""
        store = InMemoryBotStateStore()
        config = BotDetectionConfig(
            enabled=True, js_challenge={"enabled": True}
        )
        app, settings = _build_app(monkeypatch, store=store, config=config)
        token, sig = make_challenge_token(settings.secret_key)
        with TestClient(app) as client:
            r = client.post(
                "/_bot/verify",
                json={"token": token, "signature": sig},
            )
        assert r.status_code == 204
        record = asyncio.run(store.get(JS_CHALLENGE_NS, "testclient"))
        assert record is not None
        assert record.startswith("fail:")  # canvas=0 -> fails

    def test_verify_with_empty_token_does_not_record(self, monkeypatch):
        store = InMemoryBotStateStore()
        config = BotDetectionConfig(
            enabled=True, js_challenge={"enabled": True}
        )
        app, _ = _build_app(monkeypatch, store=store, config=config)
        with TestClient(app) as client:
            r = client.post(
                "/_bot/verify",
                json={"token": "", "signature": ""},
            )
        assert r.status_code == 204
        assert asyncio.run(store.get(JS_CHALLENGE_NS, "testclient")) is None
