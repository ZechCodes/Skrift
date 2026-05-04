"""Pydantic configuration for the bot detection component.

Top-level ``BotDetectionConfig`` aggregates one sub-config per metric
plus a few shared knobs. Each metric sub-config inherits from
:class:`MetricBaseConfig` and adds its own tunables. Layout mirrors
:class:`skrift.config.RateLimitConfig` so the wiring in
``skrift/config.py:get_settings`` and ``skrift/asgi.py`` looks the
same.

Phase 1 wires the top-level config and the per-metric stubs. Later
phases populate the per-metric tunables and connect the metrics
themselves.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class MetricBaseConfig(BaseModel):
    """Common shape every metric sub-config inherits."""

    enabled: bool = True


class HeadlessUAConfig(MetricBaseConfig):
    """User-Agent string inspection (phase 2)."""

    patterns: list[str] = [
        "HeadlessChrome",
        "PhantomJS",
        "Puppeteer",
        "Playwright",
        "Selenium",
    ]


class HeaderCoherenceConfig(MetricBaseConfig):
    """Header coherence check against the claimed UA (phase 2)."""

    require_sec_fetch: bool = True
    require_accept_language: bool = True


class DirectRequestConfig(MetricBaseConfig):
    """Detect requests with no navigation context (phase 2)."""

    pass


class PixelBeaconConfig(MetricBaseConfig):
    """1x1 pixel beacon to separate HTML-only fetchers from renderers (phase 4)."""

    inject_pixel: bool = True
    inject_css_beacon: bool = True
    cache_ttl: int = 60


class JSChallengeConfig(MetricBaseConfig):
    """JS-side challenge for headless detection (phase 5). Disabled by default."""

    enabled: bool = False
    block_unverified_after: int = 3
    challenge_ttl: int = 3600


class RobotsHoneypotConfig(MetricBaseConfig):
    """robots.txt + trap-link honeypot (phase 3)."""

    trap_path: str = "/private-area"
    rotate_token_days: int = 7
    log_robots_fetches: bool = True


class BotDetectionConfig(BaseModel):
    """Top-level configuration for the bot detection component."""

    enabled: bool = False
    cache_backend: Literal["redis", "memory"] = "redis"
    skip_paths: list[str] = ["/static/", "/health", "/_bot/"]
    legitimate_bot_uas: list[str] = ["Googlebot", "Bingbot", "Slackbot", "Twitterbot"]
    on_unknown: Literal["allow", "deny"] = "allow"

    headless_ua: HeadlessUAConfig = HeadlessUAConfig()
    header_coherence: HeaderCoherenceConfig = HeaderCoherenceConfig()
    direct_request: DirectRequestConfig = DirectRequestConfig()
    pixel_beacon: PixelBeaconConfig = PixelBeaconConfig()
    js_challenge: JSChallengeConfig = JSChallengeConfig()
    robots_honeypot: RobotsHoneypotConfig = RobotsHoneypotConfig()
