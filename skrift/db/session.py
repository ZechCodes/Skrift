"""ASGI middleware for SQLAlchemy session cleanup on request cancellation.

CancelledError can prevent advanced-alchemy's before_send_handler from firing,
leaving sessions unclosed and leaking connections from the pool. The middleware
in this module catches CancelledError and explicitly closes the session.
"""

import asyncio
from typing import TYPE_CHECKING

from advanced_alchemy.extensions.litestar._utils import (
    delete_aa_scope_state,
    get_aa_scope_state,
)

if TYPE_CHECKING:
    from litestar.types import ASGIApp, Receive, Scope, Send


class SessionCleanupMiddleware:
    """Ensures database sessions are closed when requests are cancelled.

    The standard advanced-alchemy before_send_handler relies on ASGI events
    (http.response.start, http.disconnect) for cleanup. CancelledError can
    prevent those events from firing, leaving sessions unclosed and leaking
    connections from the pool. This middleware catches CancelledError and
    explicitly closes the session.
    """

    def __init__(self, app: "ASGIApp", *, session_scope_key: str = "advanced_alchemy_async_session") -> None:
        self.app = app
        self.session_scope_key = session_scope_key

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except asyncio.CancelledError:
            session = get_aa_scope_state(scope, self.session_scope_key)
            if session is not None:
                await session.close()
                delete_aa_scope_state(scope, self.session_scope_key)
            raise


__all__ = ["SessionCleanupMiddleware"]
