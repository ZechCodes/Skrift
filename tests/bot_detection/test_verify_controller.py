"""Integration tests for the JS challenge /_bot/verify endpoint."""

import asyncio

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from skrift.bot_detection.challenge import (
    JS_CHALLENGE_NS,
    make_challenge_token,
)
from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.controllers import BotDetectionController
from skrift.bot_detection.hooks import BOT_CHALLENGE_PASSED
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.lib.hooks import hooks


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


@pytest.fixture
def app_with_verify(monkeypatch):
    """Litestar app with the controller wired and a known challenge config."""
    from skrift.config import Settings

    store = InMemoryBotStateStore()
    settings = Settings(
        secret_key="test-secret",
        bot_detection=BotDetectionConfig(
            enabled=True,
            js_challenge={"enabled": True, "challenge_ttl": 3600},
        ),
    )
    monkeypatch.setattr("skrift.config.get_settings", lambda: settings)

    app = Litestar(route_handlers=[BotDetectionController])
    app.state.bot_detection_store = store
    return app, store, settings


def _good_payload(token, signature):
    return {
        "token": token,
        "signature": signature,
        "webdriver": False,
        "plugins": 3,
        "languages": 2,
        "chrome": True,
        "canvas": 200,
        "timing": 500,
    }


class TestVerifyEndpoint:
    def test_passing_payload_records_pass(self, app_with_verify):
        app, store, settings = app_with_verify
        token, sig = make_challenge_token(settings.secret_key)

        with TestClient(app) as client:
            response = client.post(
                "/_bot/verify",
                json=_good_payload(token, sig),
                headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
            )
        assert response.status_code == 204
        assert asyncio.run(store.get(JS_CHALLENGE_NS, "testclient")) == "pass"

    def test_webdriver_payload_records_fail(self, app_with_verify):
        app, store, settings = app_with_verify
        token, sig = make_challenge_token(settings.secret_key)
        payload = _good_payload(token, sig)
        payload["webdriver"] = True

        with TestClient(app) as client:
            response = client.post(
                "/_bot/verify",
                json=payload,
                headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
            )
        assert response.status_code == 204
        record = asyncio.run(store.get(JS_CHALLENGE_NS, "testclient"))
        assert record is not None
        assert record.startswith("fail:")
        assert "webdriver" in record

    def test_invalid_token_does_not_record(self, app_with_verify):
        app, store, _ = app_with_verify
        with TestClient(app) as client:
            response = client.post(
                "/_bot/verify",
                json={"token": "fake", "signature": "fake"},
            )
        assert response.status_code == 204
        assert asyncio.run(store.get(JS_CHALLENGE_NS, "testclient")) is None

    def test_pass_fires_action(self, app_with_verify):
        captured = []

        async def on_pass(scope, ip, session_id):
            captured.append((ip, session_id))

        hooks.add_action(BOT_CHALLENGE_PASSED, on_pass)

        app, _, settings = app_with_verify
        token, sig = make_challenge_token(settings.secret_key)
        with TestClient(app) as client:
            client.post(
                "/_bot/verify",
                json=_good_payload(token, sig),
                headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
            )
        assert len(captured) == 1
        assert captured[0][0] == "testclient"

    def test_fail_does_not_fire_pass_action(self, app_with_verify):
        captured = []

        async def on_pass(scope, ip, session_id):
            captured.append(ip)

        hooks.add_action(BOT_CHALLENGE_PASSED, on_pass)

        app, _, settings = app_with_verify
        token, sig = make_challenge_token(settings.secret_key)
        payload = _good_payload(token, sig)
        payload["webdriver"] = True
        with TestClient(app) as client:
            client.post(
                "/_bot/verify",
                json=payload,
                headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
            )
        assert captured == []
