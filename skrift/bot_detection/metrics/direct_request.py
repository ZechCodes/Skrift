"""Detect direct requests with no navigation context.

Real users generally arrive at non-root URLs by navigation: the
request carries a ``Referer`` header, an existing session cookie, and
a ``Sec-Fetch-Site`` value other than ``none``. A request that is
missing all of these on a deep URL is almost always a curl-style
fetch.

The metric does not run on root entry-point paths (``/`` and a few
short variants) because those legitimately have no referrer for
direct-link traffic.
"""

from __future__ import annotations

from typing import ClassVar

from litestar.types import Scope

from skrift.bot_detection.config import BotDetectionConfig, DirectRequestConfig
from skrift.bot_detection.metrics.base import get_header
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult, Signal, derive_verdict

_ENTRY_PATHS = frozenset({"/", "/robots.txt", "/sitemap.xml", "/favicon.ico"})


class DirectRequestMetric:
    """Detect requests that arrive without any navigation context."""

    name: ClassVar[str] = "direct_request"

    def __init__(self, config: BotDetectionConfig) -> None:
        self._config: DirectRequestConfig = config.direct_request

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def check(
        self, scope: Scope, store: BotStateStore
    ) -> MetricResult:
        path = scope.get("path", "/")
        is_entry = path in _ENTRY_PATHS

        signals = {
            "referer": self._referer_signal(scope, is_entry),
            "session_cookie": self._session_cookie_signal(scope),
            "sec_fetch_site": self._sec_fetch_site_signal(scope, is_entry),
        }
        return MetricResult(self.name, derive_verdict(signals), signals)

    @staticmethod
    def _referer_signal(scope: Scope, is_entry: bool) -> Signal:
        referer = get_header(scope, "referer")
        if referer:
            return Signal(True)
        if is_entry:
            return Signal(None, "no Referer (entry path)")
        return Signal(False, "no Referer on deep URL")

    @staticmethod
    def _session_cookie_signal(scope: Scope) -> Signal:
        cookie = get_header(scope, "cookie") or ""
        if "session=" in cookie:
            return Signal(True)
        return Signal(None, "no session cookie")

    @staticmethod
    def _sec_fetch_site_signal(scope: Scope, is_entry: bool) -> Signal:
        value = get_header(scope, "sec-fetch-site")
        if value is None:
            return Signal(None, "Sec-Fetch-Site not sent")
        if value == "none":
            if is_entry:
                return Signal(True, "Sec-Fetch-Site: none on entry path")
            return Signal(False, "Sec-Fetch-Site: none on deep URL")
        return Signal(True, f"Sec-Fetch-Site: {value}")
