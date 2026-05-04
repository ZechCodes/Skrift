"""Tests for the JS challenge — token signing, indicator scoring, metric."""

import pytest

from skrift.bot_detection.challenge import (
    JS_CHALLENGE_NS,
    evaluate_indicators,
    make_challenge_token,
    render_challenge_tag,
    verify_challenge_token,
)
from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.metrics.js_challenge import JSChallengeMetric
from skrift.bot_detection.store import InMemoryBotStateStore


class TestChallengeToken:
    def test_round_trip(self):
        token, sig = make_challenge_token("secret")
        assert verify_challenge_token("secret", token, sig) is True

    def test_pixel_token_does_not_verify_at_challenge(self):
        from skrift.bot_detection.beacon import make_pixel_token

        token, sig = make_pixel_token("secret")
        # Different namespace -> challenge verifier rejects pixel signatures.
        assert verify_challenge_token("secret", token, sig) is False

    def test_wrong_secret_fails(self):
        token, sig = make_challenge_token("secret-a")
        assert verify_challenge_token("secret-b", token, sig) is False

    def test_each_token_is_unique(self):
        t1, _ = make_challenge_token("secret")
        t2, _ = make_challenge_token("secret")
        assert t1 != t2


class TestEvaluateIndicators:
    def _good(self, **overrides):
        baseline = {
            "webdriver": False,
            "plugins": 3,
            "languages": 2,
            "chrome": True,
            "canvas": 100,
            "timing": 500,
        }
        baseline.update(overrides)
        return baseline

    def test_passes_for_good_indicators(self):
        verdict = evaluate_indicators(
            self._good(), "Mozilla/5.0 ... Chrome/120.0"
        )
        assert verdict.passed is True

    def test_fails_when_webdriver_true(self):
        verdict = evaluate_indicators(
            self._good(webdriver=True), "Mozilla/5.0 ... Chrome/120.0"
        )
        assert verdict.passed is False
        assert "webdriver" in verdict.reason

    def test_fails_when_canvas_unavailable(self):
        verdict = evaluate_indicators(
            self._good(canvas=-1), "Mozilla/5.0 ... Chrome/120.0"
        )
        assert verdict.passed is False
        assert "canvas" in verdict.reason

    def test_fails_chromium_ua_without_window_chrome(self):
        verdict = evaluate_indicators(
            self._good(chrome=False), "Mozilla/5.0 ... Chrome/120.0"
        )
        assert verdict.passed is False
        assert "Chromium" in verdict.reason

    def test_passes_firefox_without_window_chrome(self):
        verdict = evaluate_indicators(
            self._good(chrome=False),
            "Mozilla/5.0 (X11) Gecko/20100101 Firefox/120.0",
        )
        assert verdict.passed is True

    def test_no_user_agent_does_not_crash(self):
        verdict = evaluate_indicators(self._good(), None)
        assert verdict.passed is True


class TestRenderChallengeTag:
    def test_includes_token_and_signature(self):
        html = str(render_challenge_tag("token-abc", "sig-xyz"))
        assert 'data-token="token-abc"' in html
        assert 'data-signature="sig-xyz"' in html
        assert "/static/bot_detection/challenge.js" in html

    def test_includes_nonce_when_provided(self):
        html = str(render_challenge_tag("t", "s", csp_nonce="abc123"))
        assert 'nonce="abc123"' in html

    def test_omits_nonce_when_empty(self):
        html = str(render_challenge_tag("t", "s", csp_nonce=""))
        assert "nonce=" not in html


class TestJSChallengeMetric:
    @pytest.mark.asyncio
    async def test_inconclusive_when_no_record(self):
        metric = JSChallengeMetric(
            BotDetectionConfig(js_challenge={"enabled": True})
        )
        result = await metric.check(_scope("1.2.3.4"), InMemoryBotStateStore())
        assert result.verdict is None
        assert result.signals["passed"].passed is None

    @pytest.mark.asyncio
    async def test_passes_when_pass_recorded(self):
        metric = JSChallengeMetric(
            BotDetectionConfig(js_challenge={"enabled": True})
        )
        store = InMemoryBotStateStore()
        await store.set(JS_CHALLENGE_NS, "1.2.3.4", "pass", ttl=3600)
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is True
        assert result.signals["passed"].passed is True

    @pytest.mark.asyncio
    async def test_fails_when_fail_recorded(self):
        metric = JSChallengeMetric(
            BotDetectionConfig(js_challenge={"enabled": True})
        )
        store = InMemoryBotStateStore()
        await store.set(
            JS_CHALLENGE_NS, "1.2.3.4", "fail:navigator.webdriver = true",
            ttl=3600,
        )
        result = await metric.check(_scope("1.2.3.4"), store)
        assert result.verdict is False
        assert result.signals["passed"].passed is False
        assert "webdriver" in (result.signals["passed"].detail or "")

    @pytest.mark.asyncio
    async def test_disabled_metric_reports_disabled(self):
        config = BotDetectionConfig(js_challenge={"enabled": False})
        metric = JSChallengeMetric(config)
        assert metric.enabled is False


def _scope(ip: str):
    return {
        "type": "http",
        "method": "GET",
        "path": "/page",
        "headers": [],
        "client": ("0.0.0.0", 0),
        "state": {"client_ip": ip},
    }
