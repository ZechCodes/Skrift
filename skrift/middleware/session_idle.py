"""Rolling idle-timeout for authenticated sessions (M7).

Runs after :attr:`session_config.middleware` so ``scope["session"]`` is
already populated. For authenticated sessions (those with
``SESSION_USER_ID``) the middleware:

- Evicts the session when ``int(time()) - SESSION_IDLE_LAST_SEEN`` exceeds
  the configured idle window, queuing a flash for the login page.
- Refreshes ``SESSION_IDLE_LAST_SEEN`` otherwise, throttled so chatty
  routes don't rewrite the cookie on every request.

Unauthenticated requests and non-HTTP scopes pass through untouched.
"""

from __future__ import annotations

from time import time

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.auth.session_keys import SESSION_IDLE_LAST_SEEN, SESSION_USER_ID


_IDLE_FLASH_MESSAGE = "You've been signed out due to inactivity."


class SessionIdleMiddleware:
    """Clear authenticated sessions that have been idle for too long."""

    def __init__(self, app: ASGIApp, *, idle_timeout: int) -> None:
        self.app = app
        self.idle_timeout = idle_timeout
        # Re-stamp at most once per this interval. Floor of 60s keeps
        # cookie churn down on chatty routes; the 5%-of-window cap keeps
        # the lag between real activity and stored activity small
        # relative to the idle window itself.
        self.stamp_interval = max(60, idle_timeout // 20) if idle_timeout > 0 else 60

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.idle_timeout <= 0:
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if not isinstance(session, dict) or SESSION_USER_ID not in session:
            await self.app(scope, receive, send)
            return

        now = int(time())
        last_seen = session.get(SESSION_IDLE_LAST_SEEN)

        if not isinstance(last_seen, (int, float)) or now - int(last_seen) > self.idle_timeout:
            # Idle window blown — evict the session, preserve/append a
            # flash so the next page (typically the login redirect)
            # explains why the user is anonymous now.
            flash_messages = list(session.get("flash_messages") or [])
            flash_messages.append(
                {
                    "message": _IDLE_FLASH_MESSAGE,
                    "type": "info",
                    "dismissible": True,
                }
            )
            session.clear()
            session["flash_messages"] = flash_messages
        elif now - int(last_seen) >= self.stamp_interval:
            session[SESSION_IDLE_LAST_SEEN] = now

        await self.app(scope, receive, send)
