"""CORS for the public OAuth2 authorization-server endpoints.

Browser-based OAuth clients (MCP connector setup in claude.ai, the MCP
inspector, single-page apps running authorization code + PKCE) fetch discovery,
registration and the token endpoints cross-origin. These endpoints authenticate
with bearer tokens, client secrets and PKCE, never cookies, so a wildcard origin
is safe: browsers refuse to share a wildcard-CORS response with a credentialed
request, and ``Access-Control-Allow-Credentials`` is never sent.

``/oauth/authorize`` is deliberately absent. It is a top-level navigation that
relies on the session cookie, and no cookie-authenticated route gets CORS.
"""

from __future__ import annotations

from litestar.middleware import DefineMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

OAUTH_CORS_PATHS = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
        "/oauth/register",
        "/oauth/token",
        "/oauth/jwks",
        "/oauth/revoke",
        "/oauth/introspect",
        "/oauth/userinfo",
    }
)

PREFLIGHT_MAX_AGE_SECONDS = 86400

_RESPONSE_HEADERS = [(b"access-control-allow-origin", b"*")]
_PREFLIGHT_HEADERS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
    (b"access-control-allow-headers", b"content-type, authorization"),
    (b"access-control-max-age", str(PREFLIGHT_MAX_AGE_SECONDS).encode()),
]
_CORS_HEADER_NAMES = frozenset(name for name, _ in _RESPONSE_HEADERS + _PREFLIGHT_HEADERS)


def _is_oauth_cors_path(path: str) -> bool:
    return (path.rstrip("/") or "/") in OAUTH_CORS_PATHS


def _is_preflight(scope: Scope) -> bool:
    return scope["method"] == "OPTIONS" and any(
        name.lower() == b"access-control-request-method" for name, _ in scope["headers"]
    )


class OAuthCORSMiddleware:
    """Answer CORS preflights and add ``Access-Control-Allow-Origin: *`` on
    :data:`OAUTH_CORS_PATHS`; every other path passes through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_oauth_cors_path(scope["path"]):
            await self.app(scope, receive, send)
            return

        if _is_preflight(scope):
            await send({"type": "http.response.start", "status": 204, "headers": _PREFLIGHT_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _CORS_HEADER_NAMES
                ]
                message = {**message, "headers": [*headers, *_RESPONSE_HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_cors)


def build_oauth_cors_middleware(settings) -> list[DefineMiddleware]:
    """The OAuth CORS middleware, or nothing when the authorization server is off."""
    if not settings.oauth2_enabled:
        return []
    return [DefineMiddleware(OAuthCORSMiddleware)]
