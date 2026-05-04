"""JS challenge metric — strongest positive signal for browser presence.

Reads the cross-request state written by
:meth:`BotDetectionController.verify`:

- ``js_challenge:<ip>`` = ``"pass"`` -> the browser passed the
  automation checks.
- ``js_challenge:<ip>`` = ``"fail"`` -> the browser explicitly tripped
  one of the indicator checks (e.g. ``navigator.webdriver``).
- absent -> no challenge response yet.

Like the pixel beacon, the metric does not accuse on its own when no
response has been recorded — users may have JS disabled. Sensitive
routes that *require* a JS challenge pass should opt in via
``BotGuard(require_signals=["js_challenge.passed"], on_unknown="deny")``.
"""

from __future__ import annotations

from typing import ClassVar

from litestar.types import Scope

from skrift.bot_detection.challenge import JS_CHALLENGE_NS
from skrift.bot_detection.config import BotDetectionConfig, JSChallengeConfig
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult, Signal, derive_verdict
from skrift.lib.client_ip import get_client_ip


class JSChallengeMetric:
    """Score visitors based on their JS challenge response."""

    name: ClassVar[str] = "js_challenge"

    def __init__(self, config: BotDetectionConfig) -> None:
        self._config: JSChallengeConfig = config.js_challenge

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def check(
        self, scope: Scope, store: BotStateStore
    ) -> MetricResult:
        ip = get_client_ip(scope)
        record = await store.get(JS_CHALLENGE_NS, ip)

        if record == "pass":
            signals = {"passed": Signal(True, "JS challenge passed")}
        elif record and record.startswith("fail"):
            reason = record[len("fail:") :] if ":" in record else "automation indicator tripped"
            signals = {"passed": Signal(False, f"JS challenge failed: {reason}")}
        else:
            signals = {
                "passed": Signal(None, "no JS challenge response recorded"),
            }

        return MetricResult(self.name, derive_verdict(signals), signals)
