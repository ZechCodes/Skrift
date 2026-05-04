"""Built-in bot detection metrics.

``BUILTIN_METRICS`` is the ordered list of metric classes the factory
instantiates at startup. Phase 2 ships the three passive metrics
(stateless inspection of UA + headers + cookies). Phase 3 adds the
robots-honeypot metric, phase 4 the pixel beacon, phase 5 the JS
challenge.
"""

from __future__ import annotations

from skrift.bot_detection.metrics.base import BotMetric, get_header
from skrift.bot_detection.metrics.direct_request import DirectRequestMetric
from skrift.bot_detection.metrics.header_coherence import HeaderCoherenceMetric
from skrift.bot_detection.metrics.headless_ua import HeadlessUAMetric
from skrift.bot_detection.metrics.js_challenge import JSChallengeMetric
from skrift.bot_detection.metrics.pixel_beacon import PixelBeaconMetric
from skrift.bot_detection.metrics.robots_honeypot import RobotsHoneypotMetric

BUILTIN_METRICS: list[type[BotMetric]] = [
    HeadlessUAMetric,
    HeaderCoherenceMetric,
    DirectRequestMetric,
    RobotsHoneypotMetric,
    PixelBeaconMetric,
    JSChallengeMetric,
]

__all__ = [
    "BUILTIN_METRICS",
    "BotMetric",
    "DirectRequestMetric",
    "HeaderCoherenceMetric",
    "HeadlessUAMetric",
    "JSChallengeMetric",
    "PixelBeaconMetric",
    "RobotsHoneypotMetric",
    "get_header",
]
