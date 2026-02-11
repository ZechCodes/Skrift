"""Rate limiting middleware for Skrift.

Implements per-client-IP sliding window rate limiting. Auth paths get stricter
limits, and custom per-path-prefix overrides are supported.
"""

import time

from litestar.types import ASGIApp, Receive, Scope, Send


class RateLimitMiddleware:
    """ASGI middleware that enforces per-IP request rate limits.

    Args:
        app: The ASGI application to wrap.
        requests_per_minute: Default limit for all paths.
        auth_requests_per_minute: Stricter limit for /auth/* paths.
        paths: Dict of path-prefix -> requests_per_minute overrides.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = 60,
        auth_requests_per_minute: int = 10,
        paths: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.requests_per_minute = requests_per_minute
        self.auth_requests_per_minute = auth_requests_per_minute
        self.paths = paths or {}
        # Buckets: key -> list of timestamps
        self._buckets: dict[str, list[float]] = {}
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 60.0  # seconds

    def _get_client_ip(self, scope: Scope) -> str:
        """Extract client IP, checking x-forwarded-for first."""
        headers = dict(scope.get("headers", []))
        forwarded = headers.get(b"x-forwarded-for")
        if forwarded:
            return forwarded.decode().split(",")[0].strip()
        client = scope.get("client")
        if client:
            return client[0]
        return "unknown"

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

    def _cleanup_stale(self, now: float) -> None:
        """Remove expired entries from all buckets."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - 60.0
        stale_keys = []
        for key, timestamps in self._buckets.items():
            self._buckets[key] = [t for t in timestamps if t > cutoff]
            if not self._buckets[key]:
                stale_keys.append(key)
        for key in stale_keys:
            del self._buckets[key]

    def _check_rate(self, ip: str, bucket_suffix: str, limit: int) -> tuple[bool, int]:
        """Check if request is within rate limit.

        Returns (allowed, retry_after_seconds).
        """
        now = time.monotonic()
        self._cleanup_stale(now)

        key = f"{ip}:{bucket_suffix}"
        cutoff = now - 60.0

        if key not in self._buckets:
            self._buckets[key] = []

        # Prune old entries for this bucket
        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]

        if len(self._buckets[key]) >= limit:
            oldest = self._buckets[key][0]
            retry_after = int(oldest - cutoff) + 1
            return False, max(retry_after, 1)

        self._buckets[key].append(now)
        return True, 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        ip = self._get_client_ip(scope)
        bucket_suffix, limit = self._get_limit(path)
        allowed, retry_after = self._check_rate(ip, bucket_suffix, limit)

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
