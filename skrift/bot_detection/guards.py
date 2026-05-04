"""Litestar route guards that read the bot detection result from scope state.

Two modes:

- **Default** — instantiate ``BotGuard()`` with no arguments. Blocks
  the route when the overall verdict is ``False``. Inconclusive
  verdicts (``None``) follow ``on_unknown``.

- **Per-signal** — ``BotGuard(require_signals=["headless_ua.puppeteer", ...])``.
  Each named signal must have ``passed=True``. Missing or inconclusive
  signals follow ``on_unknown``.

Examples::

    @post("/contact", guards=[BotGuard()])
    @get("/api", guards=[BotGuard(require_signals=["headless_ua.puppeteer"])])
    @get("/admin", guards=[BotGuard(require_signals=["js_challenge.passed"], on_unknown="deny")])
"""

from __future__ import annotations

from typing import Literal, Sequence

from litestar.connection import ASGIConnection
from litestar.exceptions import PermissionDeniedException
from litestar.handlers import BaseRouteHandler

from skrift.bot_detection.types import BotDetectionResult


class BotGuard:
    """Block a route when bot detection signals indicate a bot.

    Args:
        require_signals: Each entry is ``"metric.signal"``. The named
            signal must exist and have ``passed=True``. Empty (the
            default) means "trust the overall verdict".
        on_unknown: What to do when the verdict / signal is ``None``.
            ``"allow"`` lets the request through; ``"deny"`` blocks.
            ``None`` (the default) defers to the component-level
            config setting.
    """

    def __init__(
        self,
        require_signals: Sequence[str] = (),
        on_unknown: Literal["allow", "deny"] | None = None,
    ) -> None:
        self.require_signals = tuple(require_signals)
        self.on_unknown = on_unknown

    def __call__(
        self, connection: ASGIConnection, _route_handler: BaseRouteHandler
    ) -> None:
        result = self._get_result(connection)
        if result is None:
            return  # middleware not active — fail open

        on_unknown = self._resolve_on_unknown(connection)

        if not self.require_signals:
            if result.verdict is False:
                raise PermissionDeniedException(detail="Bot detection failed")
            if result.verdict is None and on_unknown == "deny":
                raise PermissionDeniedException(
                    detail="Bot detection inconclusive"
                )
            return

        for ref in self.require_signals:
            metric_name, _, signal_name = ref.partition(".")
            if not metric_name or not signal_name:
                raise ValueError(
                    f"BotGuard.require_signals entry {ref!r} must be "
                    "'metric.signal'"
                )
            metric = result.metrics.get(metric_name)
            sig = metric.signals.get(signal_name) if metric else None
            if sig is None or sig.passed is None:
                if on_unknown == "deny":
                    raise PermissionDeniedException(
                        detail=f"Bot signal unavailable: {ref}"
                    )
                continue
            if sig.passed is False:
                raise PermissionDeniedException(
                    detail=f"Bot signal failed: {ref}"
                )

    @staticmethod
    def _get_result(connection: ASGIConnection) -> BotDetectionResult | None:
        state = connection.scope.get("state")
        if not isinstance(state, dict):
            return None
        result = state.get("bot_detection")
        if isinstance(result, BotDetectionResult):
            return result
        return None

    def _resolve_on_unknown(self, connection: ASGIConnection) -> str:
        if self.on_unknown is not None:
            return self.on_unknown
        # Defer to config when the guard didn't override.
        try:
            from skrift.config import get_settings

            return get_settings().bot_detection.on_unknown
        except Exception:
            return "allow"
