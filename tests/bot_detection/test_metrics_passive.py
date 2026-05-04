"""Tests for the passive (header-only) bot detection metrics."""

import pytest

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.metrics.direct_request import DirectRequestMetric
from skrift.bot_detection.metrics.header_coherence import HeaderCoherenceMetric
from skrift.bot_detection.metrics.headless_ua import HeadlessUAMetric
from skrift.bot_detection.store import InMemoryBotStateStore


def make_scope(path="/page", headers=None):
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 0),
        "state": {},
    }


@pytest.fixture
def store():
    return InMemoryBotStateStore()


class TestHeadlessUAMetric:
    @pytest.mark.asyncio
    async def test_passes_for_real_chrome(self, store):
        metric = HeadlessUAMetric(BotDetectionConfig())
        scope = make_scope(headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0"})
        result = await metric.check(scope, store)
        assert result.verdict is True
        assert result.signals["ua_present"].passed is True
        assert result.signals["puppeteer"].passed is True
        assert result.signals["headless_chrome"].passed is True

    @pytest.mark.asyncio
    async def test_fails_for_puppeteer_ua(self, store):
        metric = HeadlessUAMetric(BotDetectionConfig())
        scope = make_scope(
            headers={"user-agent": "Mozilla/5.0 ... Chrome/120.0 Puppeteer/1.0"}
        )
        result = await metric.check(scope, store)
        assert result.verdict is False
        assert result.signals["puppeteer"].passed is False
        assert "Puppeteer" in (result.signals["puppeteer"].detail or "")

    @pytest.mark.asyncio
    async def test_fails_for_headless_chrome(self, store):
        metric = HeadlessUAMetric(BotDetectionConfig())
        scope = make_scope(
            headers={"user-agent": "Mozilla/5.0 (X11) HeadlessChrome/120.0"}
        )
        result = await metric.check(scope, store)
        assert result.verdict is False
        assert result.signals["headless_chrome"].passed is False

    @pytest.mark.asyncio
    async def test_fails_when_ua_missing(self, store):
        metric = HeadlessUAMetric(BotDetectionConfig())
        scope = make_scope()  # no UA
        result = await metric.check(scope, store)
        assert result.verdict is False
        assert result.signals["ua_present"].passed is False

    @pytest.mark.asyncio
    async def test_match_is_case_insensitive(self, store):
        metric = HeadlessUAMetric(BotDetectionConfig())
        scope = make_scope(headers={"user-agent": "headlesschrome/119"})
        result = await metric.check(scope, store)
        assert result.signals["headless_chrome"].passed is False

    @pytest.mark.asyncio
    async def test_custom_pattern_via_config(self, store):
        metric = HeadlessUAMetric(
            BotDetectionConfig(headless_ua={"patterns": ["MyBot"]})
        )
        scope = make_scope(headers={"user-agent": "MyBot/1.0"})
        result = await metric.check(scope, store)
        assert result.verdict is False
        # Slug decamelizes: "MyBot" -> "my_bot".
        assert result.signals["my_bot"].passed is False


class TestHeaderCoherenceMetric:
    @pytest.mark.asyncio
    async def test_passes_for_chromium_with_full_headers(self, store):
        metric = HeaderCoherenceMetric(BotDetectionConfig())
        scope = make_scope(
            headers={
                "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "sec-ch-ua": '"Chrome";v="120"',
                "accept-language": "en-US",
            }
        )
        result = await metric.check(scope, store)
        assert result.verdict is True
        assert result.signals["claims_modern_chromium"].passed is True
        assert result.signals["sec_fetch_site"].passed is True

    @pytest.mark.asyncio
    async def test_fails_chromium_missing_sec_fetch(self, store):
        metric = HeaderCoherenceMetric(BotDetectionConfig())
        scope = make_scope(
            headers={
                "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                "accept-language": "en-US",
            }
        )
        result = await metric.check(scope, store)
        assert result.verdict is False
        assert result.signals["sec_fetch_site"].passed is False
        assert result.signals["sec_fetch_mode"].passed is False

    @pytest.mark.asyncio
    async def test_inconclusive_for_non_chromium(self, store):
        """Firefox UAs legitimately lack Sec-Fetch headers in older versions."""
        metric = HeaderCoherenceMetric(BotDetectionConfig())
        scope = make_scope(
            headers={"user-agent": "Mozilla/5.0 (X11) Gecko/20100101 Firefox/120.0"}
        )
        result = await metric.check(scope, store)
        # No signal explicitly fails — verdict is None or True depending on
        # the claims_modern_chromium signal.
        assert result.verdict is None
        assert result.signals["sec_fetch_site"].passed is None

    @pytest.mark.asyncio
    async def test_fails_chromium_missing_accept_language(self, store):
        metric = HeaderCoherenceMetric(BotDetectionConfig())
        scope = make_scope(
            headers={
                "user-agent": "Mozilla/5.0 ... Chrome/120.0",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "sec-ch-ua": '"Chrome";v="120"',
            }
        )
        result = await metric.check(scope, store)
        assert result.signals["accept_language"].passed is False

    @pytest.mark.asyncio
    async def test_old_chrome_does_not_trigger_modern_check(self, store):
        metric = HeaderCoherenceMetric(BotDetectionConfig())
        scope = make_scope(
            headers={"user-agent": "Mozilla/5.0 ... Chrome/50.0"}
        )
        result = await metric.check(scope, store)
        assert result.signals["claims_modern_chromium"].passed is None


class TestDirectRequestMetric:
    @pytest.mark.asyncio
    async def test_passes_for_navigated_request(self, store):
        metric = DirectRequestMetric(BotDetectionConfig())
        scope = make_scope(
            "/page",
            headers={
                "referer": "https://example.com/",
                "cookie": "session=abc123",
                "sec-fetch-site": "same-origin",
            },
        )
        result = await metric.check(scope, store)
        assert result.verdict is True

    @pytest.mark.asyncio
    async def test_fails_for_curl_to_deep_url(self, store):
        metric = DirectRequestMetric(BotDetectionConfig())
        scope = make_scope("/api/sensitive", headers={"user-agent": "curl/8.0"})
        result = await metric.check(scope, store)
        assert result.verdict is False
        assert result.signals["referer"].passed is False

    @pytest.mark.asyncio
    async def test_root_path_with_no_referer_is_ok(self, store):
        metric = DirectRequestMetric(BotDetectionConfig())
        scope = make_scope("/")
        result = await metric.check(scope, store)
        # All three signals should be inconclusive for a bare root visit.
        assert result.signals["referer"].passed is None
        assert result.signals["session_cookie"].passed is None
        assert result.signals["sec_fetch_site"].passed is None
        assert result.verdict is None

    @pytest.mark.asyncio
    async def test_sec_fetch_site_none_on_deep_url_fails(self, store):
        metric = DirectRequestMetric(BotDetectionConfig())
        scope = make_scope("/page", headers={"sec-fetch-site": "none"})
        result = await metric.check(scope, store)
        assert result.signals["sec_fetch_site"].passed is False

    @pytest.mark.asyncio
    async def test_session_cookie_passes(self, store):
        metric = DirectRequestMetric(BotDetectionConfig())
        scope = make_scope("/page", headers={"cookie": "foo=bar; session=xyz"})
        result = await metric.check(scope, store)
        assert result.signals["session_cookie"].passed is True


class TestMetricRegistration:
    def test_builtin_metrics_includes_all_three_passives(self):
        from skrift.bot_detection.metrics import BUILTIN_METRICS

        names = {m.name for m in BUILTIN_METRICS}
        assert {"headless_ua", "header_coherence", "direct_request"} <= names

    @pytest.mark.asyncio
    async def test_factory_builds_enabled_metrics(self):
        from skrift.bot_detection.factory import build_initial_metrics

        config = BotDetectionConfig(enabled=True)
        metrics = build_initial_metrics(config)
        names = {m.name for m in metrics}
        assert {
            "headless_ua",
            "header_coherence",
            "direct_request",
            "robots_honeypot",
        } <= names

    @pytest.mark.asyncio
    async def test_factory_skips_disabled_metrics(self):
        from skrift.bot_detection.factory import build_initial_metrics

        config = BotDetectionConfig(
            enabled=True,
            headless_ua={"enabled": False},
        )
        metrics = build_initial_metrics(config)
        assert "headless_ua" not in {m.name for m in metrics}
