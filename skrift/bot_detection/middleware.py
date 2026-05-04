"""ASGI middleware that runs bot detection metrics and attaches the result.

The middleware is wired automatically when ``bot_detection.enabled`` is
``True`` in ``app.yaml``. It resolves the client IP from
``scope["state"]`` (set by :class:`~skrift.middleware.client_ip.ClientIPMiddleware`),
runs every enabled metric, applies the
:data:`~skrift.bot_detection.hooks.BOT_DETECTION_RESULT` filter, and
attaches a :class:`BotDetectionResult` to ``scope["state"]["bot_detection"]``
so downstream guards / handlers can read it.

Skipped paths and legitimate-bot UAs short-circuit before any metric
runs, so the high-volume static-asset path stays cheap and known good
crawlers (Googlebot etc.) are not flagged.
"""

from __future__ import annotations

import logging

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.hooks import (
    BOT_DETECTED,
    BOT_DETECTION_RESULT,
)
from skrift.bot_detection.metrics.base import BotMetric, get_header
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import (
    BotDetectionResult,
    derive_overall_verdict,
)
from skrift.lib.hooks import hooks

logger = logging.getLogger(__name__)


class BotDetectionMiddleware:
    """Attach a :class:`BotDetectionResult` to ``scope["state"]``.

    Args:
        app: The ASGI application to wrap.
        config: The resolved :class:`BotDetectionConfig`.
        store: Cross-request state backend used by deferred metrics.
        metrics: The list of metric instances to run. Built by the
            factory, possibly after running the
            :data:`~skrift.bot_detection.hooks.BOT_METRICS` filter so
            plugins can inject their own.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: BotDetectionConfig,
        store: BotStateStore,
        metrics: list[BotMetric],
    ) -> None:
        self.app = app
        self.config = config
        self.store = store
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self.config.enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if any(path.startswith(prefix) for prefix in self.config.skip_paths):
            await self.app(scope, receive, send)
            return

        # Legitimate crawlers — short-circuit with a passing verdict so
        # downstream guards do not block them. We trust the UA here;
        # reverse-DNS verification is a future enhancement.
        ua = get_header(scope, "user-agent") or ""
        if any(legit in ua for legit in self.config.legitimate_bot_uas):
            state = scope.setdefault("state", {})
            state["bot_detection"] = BotDetectionResult(verdict=True, metrics={})
            await self.app(scope, receive, send)
            return

        metrics_results = {}
        for metric in self.metrics:
            if not metric.enabled:
                continue
            try:
                result = await metric.check(scope, self.store)
            except Exception:
                logger.warning(
                    "bot_detection metric %s failed", metric.name, exc_info=True
                )
                continue
            metrics_results[metric.name] = result

        verdict = derive_overall_verdict(metrics_results)
        result = BotDetectionResult(verdict=verdict, metrics=metrics_results)
        result = await hooks.apply_filters(BOT_DETECTION_RESULT, result, scope)

        if result.verdict is False:
            await hooks.do_action(BOT_DETECTED, scope, result)

        state = scope.setdefault("state", {})
        state["bot_detection"] = result

        await self.app(scope, receive, send)
