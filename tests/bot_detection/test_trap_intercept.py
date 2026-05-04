"""Tests for the trap-path Controller (build_trap_controller)."""

import asyncio

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from skrift.bot_detection.honeypot import TRAP_HIT_NS
from skrift.bot_detection.hooks import BOT_TRAP_HIT
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.bot_detection.trap_controller import build_trap_controller
from skrift.lib.hooks import hooks


@pytest.fixture(autouse=True)
def clean_hooks_each_test():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


@pytest.fixture
def trap_app():
    store = InMemoryBotStateStore()
    controller = build_trap_controller("/private-area", store)
    app = Litestar(route_handlers=[controller])
    return app, store


class TestTrapController:
    def test_root_path_returns_404_and_records(self, trap_app):
        app, store = trap_app
        with TestClient(app) as client:
            r = client.get("/private-area")
        assert r.status_code == 404
        assert asyncio.run(store.get(TRAP_HIT_NS, "testclient")) == "/private-area"

    def test_token_path_returns_404_and_records(self, trap_app):
        app, store = trap_app
        with TestClient(app) as client:
            r = client.get("/private-area/anything")
        assert r.status_code == 404
        assert (
            asyncio.run(store.get(TRAP_HIT_NS, "testclient"))
            == "/private-area/anything"
        )

    def test_action_fires_with_token(self, trap_app):
        captured = []

        async def on_trap(scope, ip, ua, path):
            captured.append((ip, ua, path))

        hooks.add_action(BOT_TRAP_HIT, on_trap)

        app, _ = trap_app
        with TestClient(app) as client:
            client.get(
                "/private-area/abc",
                headers={"user-agent": "BadBot/1.0"},
            )
        assert captured == [("testclient", "BadBot/1.0", "/private-area/abc")]

    def test_unrelated_path_does_not_record(self, trap_app):
        app, store = trap_app
        with TestClient(app) as client:
            r = client.get("/somewhere-else")
        assert r.status_code == 404  # no handler — Litestar's 404
        assert asyncio.run(store.get(TRAP_HIT_NS, "testclient")) is None

    def test_distinct_subclass_per_call(self):
        """Two trap controllers can coexist on different paths."""
        store = InMemoryBotStateStore()
        c1 = build_trap_controller("/path-a", store)
        c2 = build_trap_controller("/path-b", store)
        assert c1 is not c2
        assert c1.path == "/path-a"
        assert c2.path == "/path-b"
