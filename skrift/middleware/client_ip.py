"""ASGI middleware that resolves the client IP using the trusted proxy model.

Runs before rate limiting and logging so downstream code reads the resolved
IP from ``scope["state"]["client_ip"]`` regardless of how many proxies sit in
front of the app.

See :mod:`skrift.lib.trusted_proxy` for the full trust model.
"""

from __future__ import annotations

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.lib.trusted_proxy import (
    StrictResolutionError,
    TrustedProxyManager,
    resolve_client_ip,
)


class ClientIPMiddleware:
    """Attach a resolved client IP to ``scope["state"]``.

    Args:
        app: The ASGI application to wrap.
        manager: Trusted-proxy manager holding the current snapshot.
    """

    def __init__(self, app: ASGIApp, manager: TrustedProxyManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        try:
            ip, source = resolve_client_ip(
                scope,
                self.manager.get(),
                client_ip_header=self.manager.config.client_ip_header,
                cdn_header=self.manager.cdn_header,
                max_hops=self.manager.config.max_hops,
                strict=self.manager.config.strict,
            )
        except StrictResolutionError as exc:
            if scope["type"] == "http":
                await _send_bad_request(send, str(exc))
                return
            # For websockets, close with a policy-violation code.
            await send({"type": "websocket.close", "code": 1008})
            return

        state = scope.setdefault("state", {})
        state["client_ip"] = ip
        state["client_ip_source"] = source

        await self.app(scope, receive, send)


async def _send_bad_request(send: Send, reason: str) -> None:
    body = f"Bad Request: {reason}".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
