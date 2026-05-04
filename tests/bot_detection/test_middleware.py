"""Tests for the BotDetectionMiddleware ASGI behaviour."""

from dataclasses import dataclass, field

import pytest

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.hooks import BOT_DETECTED, BOT_DETECTION_RESULT
from skrift.bot_detection.middleware import BotDetectionMiddleware
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.bot_detection.types import BotDetectionResult, MetricResult, Signal
from skrift.lib.hooks import hooks


@dataclass
class FakeMetric:
    name: str
    enabled: bool = True
    result: MetricResult = field(
        default_factory=lambda: MetricResult("fake", True, {})
    )

    async def check(self, scope, store):
        return self.result


@dataclass
class CrashingMetric:
    name: str = "crash"
    enabled: bool = True

    async def check(self, scope, store):
        raise RuntimeError("metric exploded")


def make_inner_app(captured):
    async def app(scope, receive, send):
        captured["scope"] = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


def make_send():
    async def send(message):
        return None

    return send


def make_scope(path="/page", headers=None):
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers or [],
        "client": ("127.0.0.1", 0),
        "state": {},
    }


@pytest.fixture(autouse=True)
def clean_hooks_each_test():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


class TestBotDetectionMiddleware:
    @pytest.mark.asyncio
    async def test_disabled_config_passes_through(self):
        captured = {}
        config = BotDetectionConfig(enabled=False)
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), []
        )
        scope = make_scope()
        await middleware(scope, None, make_send())
        assert captured["scope"]["state"].get("bot_detection") is None

    @pytest.mark.asyncio
    async def test_websocket_passes_through(self):
        captured = {}
        config = BotDetectionConfig(enabled=True)
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), []
        )
        scope = {
            "type": "websocket",
            "path": "/ws",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "state": {},
        }
        await middleware(scope, None, make_send())
        assert captured["scope"]["state"].get("bot_detection") is None

    @pytest.mark.asyncio
    async def test_skip_paths_short_circuit(self):
        captured = {}
        config = BotDetectionConfig(enabled=True, skip_paths=["/static/"])
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(),
            [FakeMetric("never_runs")],
        )
        scope = make_scope("/static/foo.css")
        await middleware(scope, None, make_send())
        assert "bot_detection" not in captured["scope"]["state"]

    @pytest.mark.asyncio
    async def test_legitimate_bot_ua_short_circuits_with_pass(self):
        captured = {}
        config = BotDetectionConfig(
            enabled=True, legitimate_bot_uas=["Googlebot"]
        )
        # Even with a metric that would say False, Googlebot should pass.
        failing = FakeMetric(
            "headless_ua",
            result=MetricResult("headless_ua", False, {"x": Signal(False)}),
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), [failing]
        )
        scope = make_scope(
            headers=[(b"user-agent", b"Mozilla/5.0 (compatible; Googlebot/2.1)")]
        )
        await middleware(scope, None, make_send())
        result = captured["scope"]["state"]["bot_detection"]
        assert result.verdict is True
        assert result.metrics == {}

    @pytest.mark.asyncio
    async def test_empty_metric_list_yields_inconclusive_verdict(self):
        captured = {}
        config = BotDetectionConfig(enabled=True)
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), []
        )
        await middleware(make_scope(), None, make_send())
        result = captured["scope"]["state"]["bot_detection"]
        assert result.verdict is None
        assert result.metrics == {}

    @pytest.mark.asyncio
    async def test_failing_metric_drives_verdict_false(self):
        captured = {}
        config = BotDetectionConfig(enabled=True)
        failing = FakeMetric(
            "headless_ua",
            result=MetricResult(
                "headless_ua", False, {"puppeteer": Signal(False, "ua")}
            ),
        )
        passing = FakeMetric(
            "direct_request",
            result=MetricResult("direct_request", True, {"ok": Signal(True)}),
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(),
            [failing, passing],
        )
        await middleware(make_scope(), None, make_send())
        result = captured["scope"]["state"]["bot_detection"]
        assert result.verdict is False
        assert result.metrics["headless_ua"].verdict is False
        assert result.metrics["direct_request"].verdict is True

    @pytest.mark.asyncio
    async def test_bot_detected_action_fires_only_on_failure(self):
        captured = {}
        called = []

        async def on_detected(scope, result):
            called.append(result)

        hooks.add_action(BOT_DETECTED, on_detected)

        config = BotDetectionConfig(enabled=True)
        failing = FakeMetric(
            "headless_ua",
            result=MetricResult("headless_ua", False, {"x": Signal(False)}),
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), [failing]
        )
        await middleware(make_scope(), None, make_send())
        assert len(called) == 1
        assert called[0].verdict is False

    @pytest.mark.asyncio
    async def test_bot_detected_action_does_not_fire_on_pass(self):
        captured = {}
        called = []

        async def on_detected(scope, result):
            called.append(result)

        hooks.add_action(BOT_DETECTED, on_detected)

        config = BotDetectionConfig(enabled=True)
        passing = FakeMetric(
            "ok", result=MetricResult("ok", True, {"x": Signal(True)})
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), [passing]
        )
        await middleware(make_scope(), None, make_send())
        assert called == []

    @pytest.mark.asyncio
    async def test_result_filter_can_override_verdict(self):
        captured = {}

        async def force_pass(result, scope):
            return BotDetectionResult(verdict=True, metrics=result.metrics)

        hooks.add_filter(BOT_DETECTION_RESULT, force_pass)

        config = BotDetectionConfig(enabled=True)
        failing = FakeMetric(
            "x", result=MetricResult("x", False, {"a": Signal(False)})
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), [failing]
        )
        await middleware(make_scope(), None, make_send())
        result = captured["scope"]["state"]["bot_detection"]
        assert result.verdict is True

    @pytest.mark.asyncio
    async def test_metric_exception_does_not_crash_middleware(self):
        captured = {}
        config = BotDetectionConfig(enabled=True)
        passing = FakeMetric(
            "ok", result=MetricResult("ok", True, {"x": Signal(True)})
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(),
            [CrashingMetric(), passing],
        )
        await middleware(make_scope(), None, make_send())
        result = captured["scope"]["state"]["bot_detection"]
        # Crashing metric is dropped; only ok remains.
        assert "crash" not in result.metrics
        assert "ok" in result.metrics

    @pytest.mark.asyncio
    async def test_disabled_metric_is_skipped(self):
        captured = {}
        config = BotDetectionConfig(enabled=True)
        disabled = FakeMetric(
            "disabled",
            enabled=False,
            result=MetricResult("disabled", False, {"x": Signal(False)}),
        )
        middleware = BotDetectionMiddleware(
            make_inner_app(captured), config, InMemoryBotStateStore(), [disabled]
        )
        await middleware(make_scope(), None, make_send())
        result = captured["scope"]["state"]["bot_detection"]
        assert "disabled" not in result.metrics
