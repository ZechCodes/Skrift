"""Pixel beacon metric — separates HTML-only fetchers from real renderers.

When a real browser parses a page it auto-fetches every ``<img>``
element. Headless browsers (Puppeteer / Playwright / Selenium) also
do. ``curl``, ``requests``, ``httpx``, and most LLM scrapers do
not — they fetch the HTML and stop.

This metric reads the cross-request pixel-load state set by
:class:`BotDetectionController`. It is a *positive-only* signal:
loading the pixel passes, but absence is reported as inconclusive.
The metric does not accuse on its own — pixel-pass strengthens the
overall verdict, pixel-absence does not weaken it.
"""

from __future__ import annotations

from typing import ClassVar

from litestar.types import Scope

from skrift.bot_detection.beacon import PIXEL_LOADED_NS
from skrift.bot_detection.config import BotDetectionConfig, PixelBeaconConfig
from skrift.bot_detection.store import BotStateStore
from skrift.bot_detection.types import MetricResult, Signal, derive_verdict
from skrift.lib.client_ip import get_client_ip


class PixelBeaconMetric:
    """Confirm a request comes from a renderer by checking pixel state."""

    name: ClassVar[str] = "pixel_beacon"

    def __init__(self, config: BotDetectionConfig) -> None:
        self._config: PixelBeaconConfig = config.pixel_beacon

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def check(
        self, scope: Scope, store: BotStateStore
    ) -> MetricResult:
        ip = get_client_ip(scope)
        loaded_via = await store.get(PIXEL_LOADED_NS, ip)

        if loaded_via:
            signals = {
                "loaded": Signal(True, f"loaded via {loaded_via}"),
            }
        else:
            signals = {
                "loaded": Signal(None, "no pixel beacon recorded yet"),
            }

        return MetricResult(self.name, derive_verdict(signals), signals)
