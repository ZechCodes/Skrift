"""HTTP endpoints owned by the bot detection component.

Phase 4 ships ``GET /_bot/p.gif`` and ``GET /_bot/c.gif`` (both
identical 1x1 GIF responses; one is referenced from ``<img>`` and one
from a CSS ``background-image`` so the metric can compare the two
beacons). Phase 5 will add ``POST /_bot/verify`` for the JS challenge.

The controller is registered conditionally from ``skrift/asgi.py``
when ``bot_detection.enabled`` is true.
"""

from __future__ import annotations

import logging
from typing import Any

from litestar import Controller, Request, get, post
from litestar.response import Response

from skrift.bot_detection.beacon import (
    PIXEL_LOADED_NS,
    verify_pixel_token,
)
from skrift.bot_detection.challenge import (
    JS_CHALLENGE_NS,
    evaluate_indicators,
    verify_challenge_token,
)
from skrift.bot_detection.hooks import BOT_CHALLENGE_PASSED, BOT_PIXEL_LOADED
from skrift.lib.client_ip import get_client_ip
from skrift.lib.hooks import hooks

logger = logging.getLogger(__name__)

# Smallest valid GIF — 43 bytes, 1x1 transparent.
_PIXEL_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9"
    b"\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    b"\x02\x02D\x01\x00;"
)


class BotDetectionController(Controller):
    """Pixel beacon endpoints for bot detection."""

    path = "/_bot"

    @get("/p.gif")
    async def pixel(self, request: Request, t: str = "", s: str = "") -> Response:
        """Image-tag pixel beacon — marks the IP as 'rendered HTML'."""
        await self._record_load(request, t, s, source="pixel")
        return _gif_response()

    @get("/c.gif")
    async def css_beacon(
        self, request: Request, t: str = "", s: str = ""
    ) -> Response:
        """CSS-loaded beacon — same payload, separate fetch path.

        Catches scrapers that load ``<img>`` tags but skip CSS, or
        vice versa. The metric will treat either one as a positive
        signal.
        """
        await self._record_load(request, t, s, source="css")
        return _gif_response()

    @post("/verify", status_code=204)
    async def verify(self, request: Request, data: dict[str, Any]) -> None:
        """Receive the JS challenge payload, score it, write the result."""
        from skrift.bot_detection.config import BotDetectionConfig
        from skrift.config import get_settings

        settings = get_settings()
        config: BotDetectionConfig = settings.bot_detection
        if not config.js_challenge.enabled:
            return

        token = str(data.get("token", ""))
        signature = str(data.get("signature", ""))
        if not verify_challenge_token(settings.secret_key, token, signature):
            logger.debug("bot_detection: challenge token verification failed")
            return

        store = _get_store(request)
        if store is None:
            return

        ua = request.headers.get("user-agent", "")
        verdict = evaluate_indicators(data, ua)
        ip = get_client_ip(request.scope)
        ttl = max(60, config.js_challenge.challenge_ttl)

        record = "pass" if verdict.passed else f"fail:{verdict.reason}"
        try:
            await store.set(JS_CHALLENGE_NS, ip, record, ttl=ttl)
        except Exception:
            logger.warning(
                "bot_detection: challenge store update failed", exc_info=True
            )
            return

        if verdict.passed:
            session_id = ""
            try:
                session = request.session
            except Exception:
                session = None
            if session:
                session_id = str(session.get("id", ""))
            await hooks.do_action(
                BOT_CHALLENGE_PASSED, request.scope, ip, session_id
            )

    async def _record_load(
        self, request: Request, token: str, signature: str, *, source: str
    ) -> None:
        """Validate the token and write state to the bot detection store."""
        from skrift.bot_detection.config import BotDetectionConfig
        from skrift.config import get_settings

        settings = get_settings()
        config: BotDetectionConfig = settings.bot_detection
        if not config.pixel_beacon.enabled:
            return

        if not verify_pixel_token(settings.secret_key, token, signature):
            logger.debug("bot_detection: pixel token verification failed")
            return

        store = _get_store(request)
        if store is None:
            return

        ip = get_client_ip(request.scope)
        try:
            await store.set(
                PIXEL_LOADED_NS,
                ip,
                source,
                ttl=max(60, config.pixel_beacon.cache_ttl * 60),
            )
        except Exception:
            logger.warning(
                "bot_detection: pixel store update failed", exc_info=True
            )
            return

        await hooks.do_action(BOT_PIXEL_LOADED, request.scope, ip, token)


def _gif_response() -> Response:
    return Response(
        content=_PIXEL_BYTES,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def _get_store(request: Request):
    """Resolve the bot state store from app state."""
    state = getattr(request.app, "state", None)
    if state is None:
        return None
    return getattr(state, "bot_detection_store", None)
