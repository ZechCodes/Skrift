"""Dynamic Controller registered for the configured trap path.

Litestar's application-level middleware does not run on unmatched
routes (the framework returns 404 before the middleware is invoked),
so a middleware-only trap interception was a no-op for arbitrary
trap URLs. Instead, this module builds a Controller subclass at app
construction time whose ``path`` is the configured ``trap_path``.

The trap handler records the hit in the bot detection state store,
fires :data:`~skrift.bot_detection.hooks.BOT_TRAP_HIT`, and then
raises ``NotFoundException`` so the response looks identical to a
normal 404 — bots get no signal that their visit was interesting.
"""

from __future__ import annotations

import logging
from typing import Type

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException

from skrift.bot_detection.honeypot import STATE_TTL_SECONDS, TRAP_HIT_NS
from skrift.bot_detection.hooks import BOT_TRAP_HIT
from skrift.bot_detection.store import BotStateStore
from skrift.lib.client_ip import get_client_ip
from skrift.lib.hooks import hooks

logger = logging.getLogger(__name__)


def build_trap_controller(
    trap_path: str, store: BotStateStore
) -> Type[Controller]:
    """Construct a Controller bound to ``trap_path``.

    Returns a fresh subclass each call (Litestar disallows duplicate
    controller registration). The store is captured by closure so the
    handler does not need to dig it back out of app state.
    """

    async def _record(request: Request) -> None:
        ip = get_client_ip(request.scope)
        ua = request.headers.get("user-agent", "")
        path = request.scope.get("path", "")
        try:
            await store.set(TRAP_HIT_NS, ip, path, ttl=STATE_TTL_SECONDS)
        except Exception:
            logger.warning(
                "bot_detection trap hit store failed", exc_info=True
            )
        await hooks.do_action(BOT_TRAP_HIT, request.scope, ip, ua, path)

    class TrapController(Controller):
        path = trap_path.rstrip("/") or "/"

        @get("/")
        async def trap_root(self, request: Request) -> None:
            await _record(request)
            raise NotFoundException()

        @get("/{token:str}")
        async def trap_with_token(
            self, request: Request, token: str
        ) -> None:
            await _record(request)
            raise NotFoundException()

    return TrapController
