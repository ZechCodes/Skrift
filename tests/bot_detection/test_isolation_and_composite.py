"""Multi-IP isolation + composite-metric scenario tests.

The deferred metrics (pixel beacon, JS challenge, robots honeypot)
all key cross-request state by client IP. Two basic correctness
properties:

1. State for IP A must not bleed into IP B's verdict.
2. Hitting the trap from one IP should not penalize a different IP.

The composite tests then put the whole stack through real-world
scenarios that combine multiple metrics, mirroring how a guard would
see traffic in production.
"""

from __future__ import annotations

import asyncio

import pytest
from litestar import Controller, Litestar, Request, get
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient

from skrift.bot_detection.beacon import make_pixel_token
from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.controllers import BotDetectionController
from skrift.bot_detection.factory import build_initial_metrics
from skrift.bot_detection.honeypot import (
    ROBOTS_READ_NS,
    TRAP_HIT_NS,
)
from skrift.bot_detection.middleware import BotDetectionMiddleware
from skrift.bot_detection.setup import setup_honeypot_hooks
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.bot_detection.trap_controller import build_trap_controller
from skrift.lib.hooks import (
    ROBOTS_TXT,
    ROBOTS_TXT_FETCHED,
    hooks,
)

SECRET = "isolation-test-secret"


class _DebugController(Controller):
    @get("/whoami")
    async def whoami(self, request: Request) -> dict:
        result = request.scope.get("state", {}).get("bot_detection")
        if result is None:
            return {"verdict": None, "metrics": {}}
        return {
            "verdict": result.verdict,
            "metric_verdicts": {
                name: m.verdict for name, m in result.metrics.items()
            },
        }

    @get("/robots.txt", media_type="text/plain")
    async def robots(self, request: Request) -> str:
        from skrift.lib.client_ip import get_client_ip

        content = "User-agent: *\nAllow: /\n"
        content = await hooks.apply_filters(ROBOTS_TXT, content)
        await hooks.do_action(
            ROBOTS_TXT_FETCHED,
            request,
            get_client_ip(request.scope),
            request.headers.get("user-agent", ""),
        )
        return content


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


@pytest.fixture
def settings(monkeypatch):
    from skrift.config import Settings

    s = Settings(
        secret_key=SECRET,
        bot_detection=BotDetectionConfig(
            enabled=True,
            cache_backend="memory",
            js_challenge={"enabled": True},
            robots_honeypot={"enabled": True, "trap_path": "/private-area"},
        ),
    )
    monkeypatch.setattr("skrift.config.get_settings", lambda: s)
    return s


@pytest.fixture
def store():
    return InMemoryBotStateStore()


@pytest.fixture
def app(settings, store):
    metrics = build_initial_metrics(settings.bot_detection)
    setup_honeypot_hooks(settings.bot_detection, store, settings.secret_key)
    trap = build_trap_controller(
        settings.bot_detection.robots_honeypot.trap_path, store
    )
    app = Litestar(
        route_handlers=[_DebugController, BotDetectionController, trap],
        middleware=[
            DefineMiddleware(
                BotDetectionMiddleware,
                config=settings.bot_detection,
                store=store,
                metrics=metrics,
            )
        ],
    )
    app.state.bot_detection_store = store
    return app


# ---------------------------------------------------------------------------
# Multi-IP isolation
# ---------------------------------------------------------------------------


class TestMultiIPIsolation:
    @pytest.mark.asyncio
    async def test_trap_hit_only_taints_the_hitting_ip(self, store):
        """Bot from IP A hits trap; IP B's verdict is unaffected."""
        await store.set(TRAP_HIT_NS, "1.1.1.1", "/private-area/x", ttl=3600)

        from skrift.bot_detection.metrics.robots_honeypot import (
            RobotsHoneypotMetric,
        )

        metric = RobotsHoneypotMetric(BotDetectionConfig())

        bot_scope = {
            "type": "http",
            "path": "/page",
            "headers": [],
            "state": {"client_ip": "1.1.1.1"},
        }
        clean_scope = {
            "type": "http",
            "path": "/page",
            "headers": [],
            "state": {"client_ip": "2.2.2.2"},
        }

        bot_result = await metric.check(bot_scope, store)
        clean_result = await metric.check(clean_scope, store)

        assert bot_result.verdict is False
        assert clean_result.verdict is None  # no signal on this IP

    @pytest.mark.asyncio
    async def test_pixel_load_only_passes_for_the_loading_ip(self, store):
        """Browser at IP A loads pixel; IP B doesn't get a free pass."""
        from skrift.bot_detection.beacon import PIXEL_LOADED_NS
        from skrift.bot_detection.metrics.pixel_beacon import (
            PixelBeaconMetric,
        )

        await store.set(PIXEL_LOADED_NS, "1.1.1.1", "pixel", ttl=3600)
        metric = PixelBeaconMetric(BotDetectionConfig())

        loader = await metric.check(
            {
                "type": "http",
                "path": "/x",
                "headers": [],
                "state": {"client_ip": "1.1.1.1"},
            },
            store,
        )
        other = await metric.check(
            {
                "type": "http",
                "path": "/x",
                "headers": [],
                "state": {"client_ip": "2.2.2.2"},
            },
            store,
        )

        assert loader.verdict is True
        assert other.verdict is None

    @pytest.mark.asyncio
    async def test_robots_fetch_isolated_per_ip(self, store):
        """Compliant bot at IP A fetched robots; IP B is untouched."""
        from skrift.bot_detection.metrics.robots_honeypot import (
            RobotsHoneypotMetric,
        )

        await store.set(ROBOTS_READ_NS, "1.1.1.1", "1", ttl=3600)
        metric = RobotsHoneypotMetric(BotDetectionConfig())

        a = await metric.check(
            {
                "type": "http",
                "path": "/x",
                "headers": [],
                "state": {"client_ip": "1.1.1.1"},
            },
            store,
        )
        b = await metric.check(
            {
                "type": "http",
                "path": "/x",
                "headers": [],
                "state": {"client_ip": "2.2.2.2"},
            },
            store,
        )

        assert a.signals["robots_txt_aware"].passed is True
        assert b.signals["robots_txt_aware"].passed is None


# ---------------------------------------------------------------------------
# Composite scenarios — a real bot trips multiple metrics
# ---------------------------------------------------------------------------


class TestCompositeBotScenarios:
    def test_puppeteer_with_no_navigation_fails_multiple_metrics(self, app):
        """Headless bot with bare-bones request — multiple metrics fail."""
        with TestClient(app) as client:
            r = client.get(
                "/whoami",
                headers={
                    "user-agent": "Mozilla/5.0 Chrome/120.0 Puppeteer/1.0",
                },
            )
        body = r.json()
        assert body["verdict"] is False
        verdicts = body["metric_verdicts"]
        # Headless UA fires explicitly; header coherence too (UA claims
        # Chromium 120 without Sec-Fetch headers); direct_request too
        # because there is no Referer / cookie / sec-fetch-site.
        assert verdicts["headless_ua"] is False
        assert verdicts["header_coherence"] is False
        assert verdicts["direct_request"] is False

    def test_real_browser_with_full_context_passes_all_passive_metrics(self, app):
        with TestClient(app) as client:
            r = client.get(
                "/whoami",
                headers={
                    "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-dest": "document",
                    "sec-ch-ua": '"Chrome";v="120"',
                    "accept-language": "en-US",
                    "referer": "https://example.com/",
                    "cookie": "session=abc",
                },
            )
        body = r.json()
        assert body["verdict"] is True
        verdicts = body["metric_verdicts"]
        assert verdicts["headless_ua"] is True
        assert verdicts["header_coherence"] is True
        assert verdicts["direct_request"] is True

    def test_compliant_bot_passes_with_robots_aware_signal(self, app, settings):
        """Visitor that fetches robots.txt + then a deep page WITH valid headers passes."""
        with TestClient(app) as client:
            client.get("/robots.txt")
            # Real-browser-style follow-up.
            r = client.get(
                "/whoami",
                headers={
                    "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-dest": "document",
                    "sec-ch-ua": '"Chrome";v="120"',
                    "accept-language": "en-US",
                    "referer": "https://example.com/",
                    "cookie": "session=abc",
                },
            )
        body = r.json()
        assert body["verdict"] is True
        assert body["metric_verdicts"]["robots_honeypot"] is True

    def test_full_bot_lifecycle_combined(self, app, store, settings):
        """Bot fetches robots, hits trap, then visits site — verdict aggregates correctly."""
        with TestClient(app) as client:
            client.get("/robots.txt")
            r = client.get("/private-area/whatever")
            assert r.status_code == 404
            r = client.get(
                "/whoami", headers={"user-agent": "BadBot/1.0"}
            )
        body = r.json()
        # Multiple metrics now agree on False.
        assert body["verdict"] is False
        verdicts = body["metric_verdicts"]
        assert verdicts["robots_honeypot"] is False
        assert verdicts["direct_request"] is False  # no Referer

    def test_browser_renders_pixel_then_passes_pixel_metric(
        self, app, store, settings
    ):
        with TestClient(app) as client:
            token, sig = make_pixel_token(settings.secret_key)
            client.get(f"/_bot/p.gif?t={token}&s={sig}")
            r = client.get(
                "/whoami",
                headers={
                    "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-dest": "document",
                    "sec-ch-ua": '"Chrome";v="120"',
                    "accept-language": "en-US",
                    "referer": "https://example.com/",
                    "cookie": "session=abc",
                },
            )
        body = r.json()
        assert body["metric_verdicts"]["pixel_beacon"] is True
        assert body["verdict"] is True
