"""Robots.txt honeypot metric.

Reads two pieces of cross-request state, both written by the trap
machinery in :mod:`skrift.bot_detection.honeypot`:

- ``robots_read:<ip>`` — set whenever the IP fetched ``robots.txt``.
- ``trap_hit:<ip>`` — set whenever the IP requested a path under the
  trap prefix.

Two signals are emitted:

- ``trap_compliance`` — fails when the IP hit the trap. The detail
  string distinguishes "fetched robots.txt then ignored the rule"
  (definitive bot) from "hit trap without reading robots.txt" (naive
  scraper).
- ``robots_txt_aware`` — informational. Passes when the IP fetched
  robots.txt; ``None`` otherwise. Real users typically never fetch
  ``robots.txt``, so a ``None`` here is normal — that is why this
  signal does not drive the verdict on its own.
"""

from __future__ import annotations

from typing import ClassVar

from litestar.types import Scope

from skrift.bot_detection.config import BotDetectionConfig, RobotsHoneypotConfig
from skrift.bot_detection.honeypot import ROBOTS_READ_NS, TRAP_HIT_NS
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult, Signal, derive_verdict
from skrift.lib.client_ip import get_client_ip


class RobotsHoneypotMetric:
    """Score visitors against the rotating-token trap rule in robots.txt."""

    name: ClassVar[str] = "robots_honeypot"

    def __init__(self, config: BotDetectionConfig) -> None:
        self._config: RobotsHoneypotConfig = config.robots_honeypot

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def check(
        self, scope: Scope, store: BotStateStore
    ) -> MetricResult:
        ip = get_client_ip(scope)
        read_robots = await store.get(ROBOTS_READ_NS, ip) is not None
        trap_hit = await store.get(TRAP_HIT_NS, ip) is not None

        if trap_hit:
            detail = (
                "hit trap path after fetching robots.txt — non-compliant bot"
                if read_robots
                else "hit trap path without reading robots.txt — naive scraper"
            )
            compliance = Signal(False, detail)
        else:
            compliance = Signal(
                True if read_robots else None,
                "robots.txt rules respected" if read_robots else None,
            )

        awareness = Signal(
            True if read_robots else None,
            "robots.txt fetched" if read_robots else None,
        )

        signals = {
            "trap_compliance": compliance,
            "robots_txt_aware": awareness,
        }
        return MetricResult(self.name, derive_verdict(signals), signals)
