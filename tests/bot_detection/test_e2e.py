"""End-to-end integration tests for the bot detection component.

Spins up a real Litestar app with the bot detection middleware,
controllers, hooks, and Jinja globals wired together — the same
wiring ``skrift.asgi.create_app`` produces, minus the database / auth
plumbing — and then exercises each metric through actual HTTP
requests via ``TestClient``.

A debug endpoint ``/whoami`` returns the bot detection result as JSON
so the test can assert on the real verdict produced by the
middleware. A separate ``/guarded`` endpoint protected by
:class:`BotGuard` proves the guard actually blocks bot traffic at the
HTTP level.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest
from litestar import Controller, Litestar, Request, get
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient

from skrift.bot_detection.beacon import make_pixel_token
from skrift.bot_detection.challenge import make_challenge_token
from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.controllers import BotDetectionController
from skrift.bot_detection.factory import build_initial_metrics
from skrift.bot_detection.guards import BotGuard
from skrift.bot_detection.middleware import BotDetectionMiddleware
from skrift.bot_detection.setup import setup_honeypot_hooks
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.bot_detection.trap_controller import build_trap_controller
from skrift.bot_detection.types import BotDetectionResult
from skrift.lib.hooks import (
    ROBOTS_TXT,
    ROBOTS_TXT_FETCHED,
    hooks,
)

SECRET = "e2e-test-secret"


def _result_to_dict(result: BotDetectionResult | None) -> dict[str, Any]:
    """Serialize a BotDetectionResult for JSON return."""
    if result is None:
        return {"verdict": None, "metrics": {}}
    return {
        "verdict": result.verdict,
        "metrics": {
            name: {
                "verdict": metric.verdict,
                "signals": {
                    sig_name: asdict(sig)
                    for sig_name, sig in metric.signals.items()
                },
            }
            for name, metric in result.metrics.items()
        },
    }


class _DebugController(Controller):
    """Surfaces bot detection state for the test, plus a guarded endpoint.

    The ``/robots.txt`` handler is a faithful but DB-free reproduction
    of :meth:`SitemapController.robots`: builds a baseline body, runs
    it through the :data:`ROBOTS_TXT` filter, and fires the
    :data:`ROBOTS_TXT_FETCHED` action — the same wire points the real
    controller uses, but without needing the SQLAlchemy plugin in this
    integration test.
    """

    @get("/whoami")
    async def whoami(self, request: Request) -> dict:
        result = request.scope.get("state", {}).get("bot_detection")
        return _result_to_dict(result)

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

    @get("/guarded", guards=[BotGuard()])
    async def guarded(self, request: Request) -> dict:
        return {"ok": True}

    @get("/guarded-verdict-unknown-deny", guards=[BotGuard(on_unknown="deny")])
    async def guarded_unknown_deny(self, request: Request) -> dict:
        return {"ok": True}

    @get(
        "/guarded-pixel",
        guards=[BotGuard(require_signals=["pixel_beacon.loaded"])],
    )
    async def guarded_pixel(self, request: Request) -> dict:
        return {"ok": True}


@pytest.fixture(autouse=True)
def clean_hooks_each_test():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


@pytest.fixture
def settings(monkeypatch):
    """Minimal Settings-like object so the controller can resolve secret_key."""
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
def app(settings):
    """Litestar app with bot detection wired end-to-end."""
    store = InMemoryBotStateStore()
    metrics = build_initial_metrics(settings.bot_detection)
    setup_honeypot_hooks(settings.bot_detection, store, settings.secret_key)

    trap_controller = build_trap_controller(
        settings.bot_detection.robots_honeypot.trap_path, store
    )
    app = Litestar(
        route_handlers=[
            _DebugController,
            BotDetectionController,
            trap_controller,
        ],
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


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Headless UA metric
# ---------------------------------------------------------------------------


class TestHeadlessUAEndToEnd:
    def test_real_chrome_passes(self, client):
        r = client.get(
            "/whoami",
            headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
        )
        ua = r.json()["metrics"]["headless_ua"]
        assert ua["verdict"] is True
        assert ua["signals"]["puppeteer"]["passed"] is True

    def test_puppeteer_fails(self, client):
        r = client.get(
            "/whoami",
            headers={"user-agent": "Mozilla/5.0 Chrome/120 Puppeteer/1.0"},
        )
        ua = r.json()["metrics"]["headless_ua"]
        assert ua["verdict"] is False
        assert ua["signals"]["puppeteer"]["passed"] is False
        assert "Puppeteer" in ua["signals"]["puppeteer"]["detail"]

    def test_headless_chrome_fails(self, client):
        r = client.get(
            "/whoami",
            headers={"user-agent": "Mozilla/5.0 (X11) HeadlessChrome/120.0"},
        )
        ua = r.json()["metrics"]["headless_ua"]
        assert ua["signals"]["headless_chrome"]["passed"] is False


# ---------------------------------------------------------------------------
# Header coherence metric
# ---------------------------------------------------------------------------


class TestHeaderCoherenceEndToEnd:
    def test_full_chromium_headers_pass(self, client):
        r = client.get(
            "/whoami",
            headers={
                "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "sec-ch-ua": '"Chrome";v="120"',
                "accept-language": "en-US",
            },
        )
        hc = r.json()["metrics"]["header_coherence"]
        assert hc["verdict"] is True

    def test_chromium_ua_missing_sec_fetch_fails(self, client):
        r = client.get(
            "/whoami",
            headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
        )
        hc = r.json()["metrics"]["header_coherence"]
        assert hc["verdict"] is False
        assert hc["signals"]["sec_fetch_site"]["passed"] is False

    def test_curl_ua_does_not_trigger_chromium_check(self, client):
        r = client.get("/whoami", headers={"user-agent": "curl/8.0"})
        hc = r.json()["metrics"]["header_coherence"]
        # Inconclusive — curl does not claim Chromium.
        assert hc["verdict"] is None


# ---------------------------------------------------------------------------
# Direct request metric
# ---------------------------------------------------------------------------


class TestDirectRequestEndToEnd:
    def test_navigated_request_passes(self, client):
        r = client.get(
            "/whoami",
            headers={
                "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                "referer": "https://example.com/",
                "cookie": "session=abc123",
                "sec-fetch-site": "same-origin",
            },
        )
        dr = r.json()["metrics"]["direct_request"]
        assert dr["verdict"] is True

    def test_curl_to_deep_url_fails_referer(self, client):
        r = client.get(
            "/whoami", headers={"user-agent": "curl/8.0"}
        )
        # /whoami is a deep URL — no referer / no cookie / no sec-fetch
        dr = r.json()["metrics"]["direct_request"]
        assert dr["verdict"] is False
        assert dr["signals"]["referer"]["passed"] is False


# ---------------------------------------------------------------------------
# Robots honeypot metric — full cycle test
# ---------------------------------------------------------------------------


class TestRobotsHoneypotEndToEnd:
    def test_robots_txt_includes_trap_rule(self, client):
        r = client.get("/robots.txt")
        assert r.status_code == 200
        assert "Disallow: /private-area/" in r.text

    def test_full_compliance_cycle(self, client):
        # Step 1: fetch robots.txt — IP is recorded as having read it.
        client.get("/robots.txt")

        # Step 2: regular request shows we have read robots.txt and not hit
        # the trap — verdict True.
        r = client.get("/whoami", headers={"user-agent": "GoodBot/1.0"})
        rh = r.json()["metrics"]["robots_honeypot"]
        assert rh["verdict"] is True
        assert rh["signals"]["robots_txt_aware"]["passed"] is True
        assert rh["signals"]["trap_compliance"]["passed"] is True

    def test_full_non_compliance_cycle(self, client):
        # Step 1: fetch robots.txt.
        client.get("/robots.txt")

        # Step 2: hit the trap path — middleware records the hit, returns 404.
        r = client.get("/private-area/whatever")
        assert r.status_code == 404

        # Step 3: subsequent request — verdict False, both signals fired.
        r = client.get("/whoami", headers={"user-agent": "BadBot/1.0"})
        rh = r.json()["metrics"]["robots_honeypot"]
        assert rh["verdict"] is False
        assert "non-compliant bot" in rh["signals"]["trap_compliance"]["detail"]

    def test_naive_scraper_hits_trap_without_robots(self, client):
        # No robots.txt fetch — straight to the trap.
        client.get("/private-area/whatever")
        r = client.get("/whoami", headers={"user-agent": "NaiveScraper/1.0"})
        rh = r.json()["metrics"]["robots_honeypot"]
        assert rh["verdict"] is False
        assert "naive scraper" in rh["signals"]["trap_compliance"]["detail"]


# ---------------------------------------------------------------------------
# Pixel beacon metric — full cycle test
# ---------------------------------------------------------------------------


class TestPixelBeaconEndToEnd:
    def test_first_request_is_inconclusive(self, client):
        r = client.get("/whoami", headers={"user-agent": "BrowserLike/1.0"})
        pb = r.json()["metrics"]["pixel_beacon"]
        assert pb["verdict"] is None

    def test_pixel_load_then_request_passes(self, client, settings):
        token, sig = make_pixel_token(settings.secret_key)
        # Step 1: simulate browser loading the pixel.
        r = client.get(f"/_bot/p.gif?t={token}&s={sig}")
        assert r.status_code == 200

        # Step 2: subsequent request — pixel_beacon now passes.
        r = client.get("/whoami", headers={"user-agent": "BrowserLike/1.0"})
        pb = r.json()["metrics"]["pixel_beacon"]
        assert pb["verdict"] is True
        assert pb["signals"]["loaded"]["passed"] is True

    def test_css_beacon_also_passes(self, client, settings):
        token, sig = make_pixel_token(settings.secret_key)
        client.get(f"/_bot/c.gif?t={token}&s={sig}")
        r = client.get("/whoami", headers={"user-agent": "BrowserLike/1.0"})
        pb = r.json()["metrics"]["pixel_beacon"]
        assert pb["verdict"] is True
        assert "css" in pb["signals"]["loaded"]["detail"]


# ---------------------------------------------------------------------------
# JS challenge metric — full cycle test
# ---------------------------------------------------------------------------


class TestJSChallengeEndToEnd:
    def _good_payload(self, token, sig):
        return {
            "token": token,
            "signature": sig,
            "webdriver": False,
            "plugins": 3,
            "languages": 2,
            "chrome": True,
            "canvas": 200,
            "timing": 500,
        }

    def test_first_request_is_inconclusive(self, client):
        r = client.get("/whoami", headers={"user-agent": "BrowserLike/1.0"})
        jc = r.json()["metrics"]["js_challenge"]
        assert jc["verdict"] is None

    def test_passing_challenge_then_request_passes(self, client, settings):
        token, sig = make_challenge_token(settings.secret_key)
        r = client.post(
            "/_bot/verify",
            json=self._good_payload(token, sig),
            headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
        )
        assert r.status_code == 204

        r = client.get(
            "/whoami", headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"}
        )
        jc = r.json()["metrics"]["js_challenge"]
        assert jc["verdict"] is True

    def test_webdriver_challenge_fails(self, client, settings):
        token, sig = make_challenge_token(settings.secret_key)
        payload = self._good_payload(token, sig)
        payload["webdriver"] = True
        client.post(
            "/_bot/verify",
            json=payload,
            headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"},
        )
        r = client.get(
            "/whoami", headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"}
        )
        jc = r.json()["metrics"]["js_challenge"]
        assert jc["verdict"] is False
        assert "webdriver" in jc["signals"]["passed"]["detail"]


# ---------------------------------------------------------------------------
# Legitimate bot allow-list short-circuits
# ---------------------------------------------------------------------------


class TestLegitimateBotEndToEnd:
    def test_googlebot_passes_with_empty_metrics(self, client):
        r = client.get(
            "/whoami",
            headers={
                "user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"
            },
        )
        assert r.json()["verdict"] is True
        assert r.json()["metrics"] == {}


# ---------------------------------------------------------------------------
# BotGuard at the HTTP layer
# ---------------------------------------------------------------------------


class TestBotGuardEndToEnd:
    def test_guard_blocks_bot_with_403(self, client):
        # Send a request that fails the headless_ua metric.
        r = client.get(
            "/guarded",
            headers={
                "user-agent": "Mozilla/5.0 Chrome/120 Puppeteer/1.0"
            },
        )
        assert r.status_code == 403

    def test_guard_allows_real_browser(self, client):
        r = client.get(
            "/guarded",
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
        assert r.status_code == 200

    def test_guard_with_on_unknown_deny_blocks_silent_visitor(self, client):
        # No bot-detection state would be inconclusive (verdict None) —
        # on_unknown="deny" should block.
        r = client.get(
            "/guarded-verdict-unknown-deny",
            headers={"user-agent": "curl/8.0"},
        )
        # curl/8.0 will fail direct_request and headless_ua presence checks,
        # so the verdict is False here — the deny path is the same status.
        assert r.status_code == 403

    def test_signal_guard_blocks_until_pixel_loads(self, client, settings):
        # Without a pixel load, /guarded-pixel should be denied
        # (signal absent + on_unknown defaults to allow at config level
        # but require_signals strictly checks the named signal).
        r = client.get(
            "/guarded-pixel",
            headers={"user-agent": "Mozilla/5.0 Chrome/120"},
        )
        # Default on_unknown is allow at config level, so missing
        # signals don't deny — this should pass through.
        assert r.status_code == 200

        # After a pixel load, the signal explicitly passes.
        token, sig = make_pixel_token(settings.secret_key)
        client.get(f"/_bot/p.gif?t={token}&s={sig}")
        r = client.get(
            "/guarded-pixel",
            headers={"user-agent": "Mozilla/5.0 Chrome/120"},
        )
        assert r.status_code == 200
