"""Rate limiting middleware for Skrift.

Enforces declarative, multi-window per-client rate limits. For each request the
middleware picks the most-specific matching rule from :class:`RateLimitConfig`
and evaluates it through the shared :class:`~skrift.ratelimit.RateLimiter`, so
the backend (in-memory vs Redis) is chosen in exactly one place and every
limiter agrees.

Static route policy is pure config — see ``rate_limit.rules`` in app.yaml. For
dynamic or per-handler limits, call ``get_limiter().check(...)`` directly.
"""

from __future__ import annotations

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.config import RateLimitConfig
from skrift.lib.client_ip import get_client_ip
from skrift.ratelimit import RateLimiter


def _resolve_caller_key(scope: Scope, key_kind: str) -> str:
    """Derive the caller-identity string for a rule's ``key`` strategy.

    ``ip`` uses the resolved client IP. ``api_key`` uses the bearer token /
    ``X-API-Key`` header, falling back to the client IP for anonymous callers
    so an api_key-keyed rule still limits unauthenticated traffic.
    """
    if key_kind == "api_key":
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"")
        if authorization.lower().startswith(b"bearer "):
            return "apikey:" + authorization[7:].decode("latin-1").strip()
        api_key_header = headers.get(b"x-api-key", b"")
        if api_key_header:
            return "apikey:" + api_key_header.decode("latin-1").strip()
    return get_client_ip(scope)


class RateLimitMiddleware:
    """ASGI middleware that enforces per-client multi-window rate limits.

    Args:
        app: The ASGI application to wrap.
        config: The rate-limit configuration. Built from the legacy keyword
            arguments when omitted.
        limiter: The shared rate limiter. An in-memory limiter is created when
            omitted — convenient for tests.
        requests_per_minute: Legacy default limit (used only when ``config``
            is not supplied).
        auth_requests_per_minute: Legacy stricter limit for ``/auth`` paths.
        paths: Legacy dict of path-prefix -> requests_per_minute overrides.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: RateLimitConfig | None = None,
        limiter: RateLimiter | None = None,
        requests_per_minute: int = 60,
        auth_requests_per_minute: int = 10,
        paths: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.config = config or RateLimitConfig(
            requests_per_minute=requests_per_minute,
            auth_requests_per_minute=auth_requests_per_minute,
            paths=paths or {},
        )
        # An in-memory limiter keeps the middleware self-contained for tests
        # and apps that never set redis.url.
        self.limiter = limiter or RateLimiter(redis_client=None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET")
        rule = self.config.resolve(path, method)
        caller_key = _resolve_caller_key(scope, rule.key)
        verdict = await self.limiter.check(rule.name, caller_key, rule.limits)

        if not verdict.allowed:
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"retry-after", str(verdict.retry_after).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"Too Many Requests",
            })
            return

        await self.app(scope, receive, send)
