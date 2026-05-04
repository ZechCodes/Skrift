"""Tests for plugin extensibility — custom metrics + on_startup metric filter.

Validates the two main extension paths a plugin author would use:

- Register a ``BOT_METRICS`` filter handler that injects a custom
  metric instance into the list run on every request.
- Have ``apply_metrics_filter`` (called from ``on_startup``) actually
  pick that handler up and mutate the list passed to the middleware
  in place.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.factory import (
    apply_metrics_filter,
    build_initial_metrics,
)
from skrift.bot_detection.hooks import BOT_METRICS
from skrift.bot_detection.middleware import BotDetectionMiddleware
from skrift.bot_detection.store import InMemoryBotStateStore
from skrift.bot_detection.types import MetricResult, Signal
from skrift.lib.hooks import hooks


class CustomMetric:
    """Test-only metric that fails the verdict for any request from a banned IP."""

    name: ClassVar[str] = "ban_list"

    def __init__(self, banned_ips: set[str]):
        self._banned = banned_ips
        self.enabled = True

    async def check(self, scope, store):
        ip = ""
        state = scope.get("state", {})
        if isinstance(state, dict):
            ip = state.get("client_ip", "") or ""
        if not ip:
            client = scope.get("client")
            if client:
                ip = client[0]

        if ip in self._banned:
            sig = Signal(False, f"{ip} on ban list")
        else:
            sig = Signal(True)
        return MetricResult(self.name, sig.passed, {"banned": sig})


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks._actions.clear()
    hooks._filters.clear()
    yield
    hooks._actions.clear()
    hooks._filters.clear()


class TestApplyMetricsFilter:
    @pytest.mark.asyncio
    async def test_filter_can_append_custom_metric(self):
        async def add_custom(metrics, config):
            return metrics + [CustomMetric(banned_ips={"5.5.5.5"})]

        hooks.add_filter(BOT_METRICS, add_custom)

        config = BotDetectionConfig(enabled=True)
        metrics = build_initial_metrics(config)
        original_count = len(metrics)
        await apply_metrics_filter(metrics, config)

        assert len(metrics) == original_count + 1
        names = {m.name for m in metrics}
        assert "ban_list" in names

    @pytest.mark.asyncio
    async def test_filter_can_replace_metric_list(self):
        async def replace_all(metrics, config):
            return [CustomMetric(banned_ips=set())]

        hooks.add_filter(BOT_METRICS, replace_all)

        config = BotDetectionConfig(enabled=True)
        metrics = build_initial_metrics(config)
        await apply_metrics_filter(metrics, config)

        assert len(metrics) == 1
        assert metrics[0].name == "ban_list"

    @pytest.mark.asyncio
    async def test_filter_chains_in_priority_order(self):
        order: list[str] = []

        async def first(metrics, config):
            order.append("first")
            return metrics

        async def second(metrics, config):
            order.append("second")
            return metrics

        hooks.add_filter(BOT_METRICS, first, priority=10)
        hooks.add_filter(BOT_METRICS, second, priority=5)

        config = BotDetectionConfig(enabled=True)
        metrics = build_initial_metrics(config)
        await apply_metrics_filter(metrics, config)

        # Lower priority runs first.
        assert order == ["second", "first"]

    @pytest.mark.asyncio
    async def test_filter_mutates_list_in_place(self):
        """The middleware holds a reference; the filter must update that list."""
        async def add_custom(metrics, config):
            return metrics + [CustomMetric(banned_ips={"1.1.1.1"})]

        hooks.add_filter(BOT_METRICS, add_custom)

        config = BotDetectionConfig(enabled=True)
        metrics = build_initial_metrics(config)
        original_id = id(metrics)
        await apply_metrics_filter(metrics, config)
        # Same list object — mutated in place so the middleware reference stays valid.
        assert id(metrics) == original_id


class TestCustomMetricEndToEnd:
    @pytest.mark.asyncio
    async def test_custom_metric_drives_middleware_verdict(self):
        async def add_custom(metrics, config):
            return metrics + [CustomMetric(banned_ips={"5.5.5.5"})]

        hooks.add_filter(BOT_METRICS, add_custom)

        config = BotDetectionConfig(enabled=True)
        store = InMemoryBotStateStore()
        metrics = build_initial_metrics(config)
        await apply_metrics_filter(metrics, config)

        captured: dict = {}

        async def app(scope, receive, send):
            captured["scope"] = scope
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def send(message):
            return None

        middleware = BotDetectionMiddleware(app, config, store, metrics)

        # Banned IP -> custom metric fails -> verdict False.
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/page",
            "headers": [(b"user-agent", b"Mozilla/5.0 ... Chrome/120.0")],
            "client": ("5.5.5.5", 0),
            "state": {"client_ip": "5.5.5.5"},
        }
        await middleware(scope, None, send)
        result = captured["scope"]["state"]["bot_detection"]
        assert result.metrics["ban_list"].verdict is False
        assert result.verdict is False

    @pytest.mark.asyncio
    async def test_unbanned_ip_passes_custom_metric(self):
        async def add_custom(metrics, config):
            return metrics + [CustomMetric(banned_ips={"5.5.5.5"})]

        hooks.add_filter(BOT_METRICS, add_custom)

        config = BotDetectionConfig(enabled=True)
        store = InMemoryBotStateStore()
        metrics = build_initial_metrics(config)
        await apply_metrics_filter(metrics, config)

        captured: dict = {}

        async def app(scope, receive, send):
            captured["scope"] = scope
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def send(message):
            return None

        middleware = BotDetectionMiddleware(app, config, store, metrics)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (b"user-agent", b"Mozilla/5.0 ... Chrome/120.0"),
                (b"sec-fetch-site", b"same-origin"),
                (b"sec-fetch-mode", b"navigate"),
                (b"sec-fetch-dest", b"document"),
                (b"sec-ch-ua", b'"Chrome";v="120"'),
                (b"accept-language", b"en-US"),
                (b"referer", b"https://example.com/"),
                (b"cookie", b"session=abc"),
            ],
            "client": ("9.9.9.9", 0),
            "state": {"client_ip": "9.9.9.9"},
        }
        await middleware(scope, None, send)
        result = captured["scope"]["state"]["bot_detection"]
        assert result.metrics["ban_list"].verdict is True
