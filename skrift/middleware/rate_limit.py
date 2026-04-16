"""Rate limiting middleware for Skrift.

Implements per-client-IP sliding window rate limiting. Auth paths get stricter
limits, and custom per-path-prefix overrides are supported.

The counter backend is injected — in-memory by default, Redis when configured.
See :mod:`skrift.lib.sliding_window` and
:mod:`skrift.lib.sliding_window_redis`.
"""

from __future__ import annotations

from litestar.types import ASGIApp, Receive, Scope, Send

from skrift.lib.client_ip import get_client_ip
from skrift.lib.sliding_window import InMemorySlidingWindowCounter, SlidingWindowCounter


class RateLimitMiddleware:
    """ASGI middleware that enforces per-IP request rate limits.

    Args:
        app: The ASGI application to wrap.
        requests_per_minute: Default limit for all paths.
        auth_requests_per_minute: Stricter limit for /auth/* paths.
        paths: Dict of path-prefix -> requests_per_minute overrides.
        counter: Optional pre-built counter. Falls back to an in-memory
            counter when ``None`` — convenient for tests.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = 60,
        auth_requests_per_minute: int = 10,
        paths: dict[str, int] | None = None,
        counter: SlidingWindowCounter | None = None,
    ) -> None:
        self.app = app
        self.requests_per_minute = requests_per_minute
        self.auth_requests_per_minute = auth_requests_per_minute
        self.paths = paths or {}
        self._counter: SlidingWindowCounter = counter or InMemorySlidingWindowCounter(window=60.0)

    def _get_limit(self, path: str) -> tuple[str, int]:
        """Determine the rate limit and bucket suffix for a path.

        Returns (bucket_suffix, limit_per_minute).
        """
        # Check custom path prefixes first (longest match wins)
        best_match = ""
        for prefix, limit in self.paths.items():
            if path.startswith(prefix) and len(prefix) > len(best_match):
                best_match = prefix

        if best_match:
            return best_match, self.paths[best_match]

        # Auth paths get stricter limits
        if path.startswith("/auth"):
            return "/auth", self.auth_requests_per_minute

        return "", self.requests_per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        ip = get_client_ip(scope)
        bucket_suffix, limit = self._get_limit(path)
        key = f"{ip}:{bucket_suffix}"
        allowed, retry_after = await self._counter.check_and_record(key, limit)

        if not allowed:
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"retry-after", str(retry_after).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"Too Many Requests",
            })
            return

        await self.app(scope, receive, send)
