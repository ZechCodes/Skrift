"""Headless / automation framework detection by User-Agent inspection.

Emits one signal per configured pattern. A signal *fails* when the UA
contains the pattern (case-insensitive) — that is the explicit
positive indicator of a headless browser. A separate ``ua_present``
signal fails when the UA header is missing or empty: most legitimate
clients send one and a missing UA is itself a weak bot indicator.

This metric is cheap and stateless. It is also trivially spoofable —
treat it as one input among several rather than a hard block.
"""

from __future__ import annotations

import re
from typing import ClassVar

from litestar.types import Scope

from skrift.bot_detection.config import BotDetectionConfig, HeadlessUAConfig
from skrift.bot_detection.metrics.base import get_header
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult, Signal, derive_verdict

_CAMEL_CASE_SPLIT = re.compile(r"([a-z])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _signal_name(pattern: str) -> str:
    """Stable lowercase slug derived from a pattern name.

    ``HeadlessChrome`` -> ``headless_chrome``, ``PhantomJS`` ->
    ``phantom_js``, ``Puppeteer`` -> ``puppeteer``.
    """
    decamelized = _CAMEL_CASE_SPLIT.sub(r"\1_\2", pattern)
    return _NON_ALNUM.sub("_", decamelized.lower()).strip("_")


class HeadlessUAMetric:
    """Emit a pass/fail signal per configured headless-browser pattern."""

    name: ClassVar[str] = "headless_ua"

    def __init__(self, config: BotDetectionConfig) -> None:
        self._config: HeadlessUAConfig = config.headless_ua

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def check(
        self, scope: Scope, store: BotStateStore
    ) -> MetricResult:
        ua = get_header(scope, "user-agent")
        signals: dict[str, Signal] = {}

        if ua is None or ua == "":
            signals["ua_present"] = Signal(False, "User-Agent header is missing")
        else:
            signals["ua_present"] = Signal(True)
            ua_lower = ua.lower()
            for pattern in self._config.patterns:
                signal_name = _signal_name(pattern)
                if pattern.lower() in ua_lower:
                    signals[signal_name] = Signal(
                        False, f"User-Agent contains {pattern!r}"
                    )
                else:
                    signals[signal_name] = Signal(True)

        return MetricResult(self.name, derive_verdict(signals), signals)
