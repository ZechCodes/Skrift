"""Pluggable bot / scraper detection for Skrift.

Configurable per-metric detection that attaches a structured
verdict + per-signal evidence to ``scope["state"]["bot_detection"]``.
End users either trust the rolled-up verdict or read raw signals to
build their own decision logic. Route guards wrap both paths so a
handler can be gated on the verdict, on specific signals, or on a
mix.

Public surface::

    from skrift.bot_detection import (
        BotDetectionConfig,
        BotDetectionResult,
        BotGuard,
        MetricResult,
        Signal,
    )

Hook constants are re-exported from :mod:`skrift.bot_detection.hooks`.
"""

from __future__ import annotations

from skrift.bot_detection.config import BotDetectionConfig
from skrift.bot_detection.guards import BotGuard
from skrift.bot_detection.hooks import (
    BOT_CHALLENGE_PASSED,
    BOT_DETECTED,
    BOT_DETECTION_RESULT,
    BOT_METRICS,
    BOT_PIXEL_LOADED,
    BOT_TRAP_HIT,
)
from skrift.bot_detection.metrics.base import BotMetric
from skrift.bot_detection.types import (
    BotDetectionResult,
    MetricResult,
    Signal,
)

__all__ = [
    "BOT_CHALLENGE_PASSED",
    "BOT_DETECTED",
    "BOT_DETECTION_RESULT",
    "BOT_METRICS",
    "BOT_PIXEL_LOADED",
    "BOT_TRAP_HIT",
    "BotDetectionConfig",
    "BotDetectionResult",
    "BotGuard",
    "BotMetric",
    "MetricResult",
    "Signal",
]
