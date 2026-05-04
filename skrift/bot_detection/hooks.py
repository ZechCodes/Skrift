"""Hook constants exposed by the bot detection component.

These names are also defined in :mod:`skrift.lib.hooks` (alongside the
other Skrift hook constants for discoverability). They live here too
so that anything importing the component does not need to reach into
``skrift.lib.hooks`` for the names.

Filters
-------
``BOT_METRICS`` — startup filter. Receives ``list[BotMetric]`` (the
built-ins for which ``enabled=True``) and returns the final list to
run. Plugins prepend / append / replace their own metrics here.

``BOT_DETECTION_RESULT`` — per-request filter. Receives the assembled
:class:`~skrift.bot_detection.types.BotDetectionResult` after every
metric has run, before guards see it. Plugins can override the
verdict, add metrics, or strip signals.

Actions
-------
``BOT_DETECTED`` — fires once per request when the overall verdict is
``False``. Args: ``(scope, result)``. Use it for logging, banning,
alerting.

``BOT_TRAP_HIT`` — fires when the robots-honeypot trap path is hit.
Args: ``(scope, ip, ua, token)``.

``BOT_PIXEL_LOADED`` — fires when the pixel beacon is fetched. Args:
``(scope, ip, request_id)``.

``BOT_CHALLENGE_PASSED`` — fires when the JS challenge succeeds. Args:
``(scope, ip, session_id)``.
"""

from __future__ import annotations

# Filters
BOT_METRICS = "bot_metrics"
BOT_DETECTION_RESULT = "bot_detection_result"

# Actions
BOT_DETECTED = "bot_detected"
BOT_TRAP_HIT = "bot_trap_hit"
BOT_PIXEL_LOADED = "bot_pixel_loaded"
BOT_CHALLENGE_PASSED = "bot_challenge_passed"
