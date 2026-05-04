"""Integration tests for the BotDetectionController pixel endpoints."""

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from skrift.bot_detection.beacon import (
    PIXEL_LOADED_NS,
    make_pixel_token,
)
from skrift.bot_detection.controllers import BotDetectionController
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.bot_detection.hooks import BOT_PIXEL_LOADED
from skrift.lib.hooks import hooks


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


@pytest.fixture
def app_with_pixel(tmp_path, monkeypatch):
    """Build a Litestar app with the bot detection controller wired up.

    Patches ``get_settings`` to return a minimal Settings with
    ``bot_detection.enabled=True`` and a known secret, and stashes the
    store on app state.
    """
    from skrift.bot_detection.config import BotDetectionConfig
    from skrift.config import Settings

    store = InMemoryBotStateStore()
    settings = Settings(
        secret_key="test-secret",
        bot_detection=BotDetectionConfig(enabled=True),
    )
    monkeypatch.setattr(
        "skrift.config.get_settings", lambda: settings
    )

    app = Litestar(route_handlers=[BotDetectionController])
    app.state.bot_detection_store = store
    return app, store, settings


class TestPixelEndpoint:
    def test_returns_gif_with_no_cache_headers(self, app_with_pixel):
        app, _, settings = app_with_pixel
        token, sig = make_pixel_token(settings.secret_key)
        with TestClient(app) as client:
            response = client.get(f"/_bot/p.gif?t={token}&s={sig}")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/gif"
        assert response.content[:3] == b"GIF"
        assert "no-store" in response.headers.get("cache-control", "")

    def test_records_load_with_valid_token(self, app_with_pixel):
        app, store, settings = app_with_pixel
        token, sig = make_pixel_token(settings.secret_key)
        with TestClient(app) as client:
            response = client.get(f"/_bot/p.gif?t={token}&s={sig}")
        assert response.status_code == 200

        # Synchronous check via the in-memory store.
        import asyncio

        loaded = asyncio.run(store.get(PIXEL_LOADED_NS, "testclient"))
        assert loaded == "pixel"

    def test_invalid_token_does_not_record(self, app_with_pixel):
        app, store, _ = app_with_pixel
        with TestClient(app) as client:
            response = client.get("/_bot/p.gif?t=fake&s=fake")
        assert response.status_code == 200

        import asyncio
        loaded = asyncio.run(store.get(PIXEL_LOADED_NS, "testclient"))
        assert loaded is None

    def test_css_beacon_records_separately(self, app_with_pixel):
        app, store, settings = app_with_pixel
        token, sig = make_pixel_token(settings.secret_key)
        with TestClient(app) as client:
            response = client.get(f"/_bot/c.gif?t={token}&s={sig}")
        assert response.status_code == 200

        import asyncio
        loaded = asyncio.run(store.get(PIXEL_LOADED_NS, "testclient"))
        assert loaded == "css"

    def test_pixel_load_fires_action(self, app_with_pixel):
        captured = []

        async def on_load(scope, ip, token):
            captured.append((ip, token))

        hooks.add_action(BOT_PIXEL_LOADED, on_load)

        app, _, settings = app_with_pixel
        token, sig = make_pixel_token(settings.secret_key)
        with TestClient(app) as client:
            client.get(f"/_bot/p.gif?t={token}&s={sig}")

        assert len(captured) == 1
        ip, recorded_token = captured[0]
        assert ip == "testclient"
        assert recorded_token == token
