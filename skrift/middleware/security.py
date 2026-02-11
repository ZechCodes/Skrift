"""Security headers middleware for Skrift.

Injects security response headers (CSP, HSTS, X-Frame-Options, etc.)
into every HTTP response. Headers already set by a route handler are
not overwritten, allowing per-route overrides.
"""

from litestar.types import ASGIApp, Receive, Scope, Send


class SecurityHeadersMiddleware:
    """ASGI middleware that adds security headers to HTTP responses.

    Args:
        app: The ASGI application to wrap.
        headers: Pre-encoded header pairs as list of (name_bytes, value_bytes).
        debug: Whether the application is running in debug mode.
    """

    def __init__(self, app: ASGIApp, headers: list[tuple[bytes, bytes]], debug: bool = False) -> None:
        self.app = app
        self.headers = headers
        self.debug = debug

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                existing = {h[0].lower() for h in message.get("headers", [])}
                extra = [(k, v) for k, v in self.headers if k.lower() not in existing]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)
