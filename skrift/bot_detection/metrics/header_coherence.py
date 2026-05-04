"""Header coherence check — does the request match what the UA claims?

Modern Chromium-based browsers (Chrome 80+, Edge 80+) reliably send
``Sec-Fetch-*``, ``Sec-CH-UA``, ``Accept-Language`` and
``Accept-Encoding``. A request whose User-Agent claims to be one of
those browsers but is missing the headers is almost certainly a
script — automation libraries and naive scrapers commonly forge the
UA but omit everything else.

Signals are inconclusive (``None``) when the UA does not claim a
modern Chromium, since older browsers and non-Chromium clients
legitimately omit these headers.
"""

from __future__ import annotations

import re
from typing import ClassVar

from litestar.types import Scope

from skrift.bot_detection.config import BotDetectionConfig, HeaderCoherenceConfig
from skrift.bot_detection.metrics.base import get_header
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult, Signal, derive_verdict

# Match Chromium-based UAs that are recent enough to send Sec-* headers.
_CHROMIUM_RE = re.compile(r"\b(?:Chrome|Edg|OPR)/(\d+)\.")
_CHROMIUM_MIN_VERSION = 80


class HeaderCoherenceMetric:
    """Emit signals comparing request headers against the claimed UA."""

    name: ClassVar[str] = "header_coherence"

    def __init__(self, config: BotDetectionConfig) -> None:
        self._config: HeaderCoherenceConfig = config.header_coherence

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def check(
        self, scope: Scope, store: BotStateStore
    ) -> MetricResult:
        ua = get_header(scope, "user-agent") or ""
        match = _CHROMIUM_RE.search(ua)
        is_modern_chromium = bool(
            match and int(match.group(1)) >= _CHROMIUM_MIN_VERSION
        )

        signals: dict[str, Signal] = {
            "claims_modern_chromium": Signal(
                True if is_modern_chromium else None,
                f"User-Agent: {ua!r}" if ua else "User-Agent missing",
            )
        }

        # If the UA does not claim modern Chromium, the rest of the
        # signals are inconclusive — a Firefox / Safari / curl client
        # legitimately omits Sec-Fetch-*.
        outcome = True if is_modern_chromium else None

        if self._config.require_sec_fetch:
            for header in ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"):
                signal_key = header.replace("-", "_")
                signals[signal_key] = self._check_header_present(
                    scope, header, outcome
                )
            signals["sec_ch_ua"] = self._check_header_present(
                scope, "sec-ch-ua", outcome
            )

        if self._config.require_accept_language:
            signals["accept_language"] = self._check_header_present(
                scope, "accept-language", outcome
            )

        return MetricResult(self.name, derive_verdict(signals), signals)

    @staticmethod
    def _check_header_present(
        scope: Scope, header: str, expected_outcome: bool | None
    ) -> Signal:
        """Pass when the header is present.

        When ``expected_outcome`` is ``True`` (we expect this header for
        the claimed UA) and the header is missing, fail with a useful
        detail. When ``expected_outcome`` is ``None`` (we have no
        expectation), report ``None`` regardless of presence.
        """
        present = get_header(scope, header) is not None
        if present:
            return Signal(True)
        if expected_outcome is None:
            return Signal(None)
        return Signal(False, f"missing {header} header")
