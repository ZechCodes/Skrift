"""Security headers middleware for Skrift.

Injects security response headers (CSP, HSTS, X-Frame-Options, etc.)
into every HTTP response. Headers already set by a route handler are
not overwritten, allowing per-route overrides.

When csp_nonce is enabled, 'unsafe-inline' in the style-src directive
is replaced with a per-request nonce value.
"""

import contextvars
import re
import secrets

from litestar.types import ASGIApp, Receive, Scope, Send

# ContextVar for template access to the current request's CSP nonce
csp_nonce_var: contextvars.ContextVar[str] = contextvars.ContextVar("csp_nonce")

_STYLE_SRC_UNSAFE_INLINE = re.compile(r"(style-src\s[^;]*)'unsafe-inline'")


class SecurityHeadersMiddleware:
    """ASGI middleware that adds security headers to HTTP responses.

    Args:
        app: The ASGI application to wrap.
        headers: Pre-encoded header pairs as list of (name_bytes, value_bytes).
            Should NOT include CSP (CSP is handled separately via csp_value).
        csp_value: The raw CSP header string (or None to disable CSP).
        csp_nonce: Whether to replace 'unsafe-inline' in style-src with a nonce.
        debug: Whether the application is running in debug mode.
    """

    def __init__(
        self,
        app: ASGIApp,
        headers: list[tuple[bytes, bytes]],
        csp_value: str | None = None,
        csp_nonce: bool = True,
        debug: bool = False,
    ) -> None:
        self.app = app
        self.headers = headers
        self.csp_value = csp_value
        self.csp_nonce = csp_nonce
        self.debug = debug

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        nonce: str | None = None
        token = None

        if self.csp_nonce and self.csp_value:
            nonce = secrets.token_urlsafe(16)
            # Store in scope state for other middleware/handlers
            scope.setdefault("state", {})
            scope["state"]["csp_nonce"] = nonce
            token = csp_nonce_var.set(nonce)

        try:
            # Build CSP header for this request
            csp_header: tuple[bytes, bytes] | None = None
            if self.csp_value:
                if nonce:
                    csp_str = _STYLE_SRC_UNSAFE_INLINE.sub(
                        rf"\1'nonce-{nonce}'", self.csp_value
                    )
                else:
                    csp_str = self.csp_value
                csp_header = (b"content-security-policy", csp_str.encode())

            async def send_with_headers(message: dict) -> None:
                if message["type"] == "http.response.start":
                    existing = {h[0].lower() for h in message.get("headers", [])}
                    extra = [(k, v) for k, v in self.headers if k.lower() not in existing]
                    if csp_header and csp_header[0] not in existing:
                        extra.append(csp_header)
                    message["headers"] = list(message.get("headers", [])) + extra
                await send(message)

            await self.app(scope, receive, send_with_headers)
        finally:
            if token is not None:
                csp_nonce_var.reset(token)
