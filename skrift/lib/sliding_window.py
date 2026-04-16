"""Per-key sliding window counter.

Used for rate limiting and failed-auth tracking. Two backends implement the
:class:`SlidingWindowCounter` protocol:

* :class:`InMemorySlidingWindowCounter` — process-local dict. Fast, but each
  worker keeps its own counts, so limits don't add up across replicas.
* :class:`~skrift.lib.sliding_window_redis.RedisSlidingWindowCounter` —
  Redis-backed sorted sets, atomic via Lua. Counts are shared across
  replicas.

Callers see an async interface so the middleware doesn't need to care
which backend it's talking to.
"""

from __future__ import annotations

import time
from typing import Protocol


class SlidingWindowCounter(Protocol):
    """Async interface both backends implement."""

    async def check_and_record(self, key: str, limit: int) -> tuple[bool, int]: ...

    async def record(self, key: str) -> None: ...

    async def count(self, key: str) -> int: ...


class InMemorySlidingWindowCounter:
    """Process-local sliding window counter.

    Tracks per-key hit counts within a sliding time window. Periodically
    prunes stale entries to bound memory usage.
    """

    def __init__(self, window: float = 60.0, cleanup_interval: float = 60.0) -> None:
        self.window = window
        self._buckets: dict[str, list[float]] = {}
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = cleanup_interval

    def _cleanup_stale(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - self.window
        stale_keys = []
        for key, timestamps in self._buckets.items():
            self._buckets[key] = [t for t in timestamps if t > cutoff]
            if not self._buckets[key]:
                stale_keys.append(key)
        for key in stale_keys:
            del self._buckets[key]

    async def record(self, key: str) -> None:
        """Record a hit for *key*."""
        now = time.monotonic()
        self._cleanup_stale(now)
        self._buckets.setdefault(key, []).append(now)

    async def count(self, key: str) -> int:
        """Return the number of hits for *key* within the current window."""
        now = time.monotonic()
        self._cleanup_stale(now)
        cutoff = now - self.window
        timestamps = self._buckets.get(key)
        if not timestamps:
            return 0
        self._buckets[key] = [t for t in timestamps if t > cutoff]
        return len(self._buckets[key])

    async def check_and_record(self, key: str, limit: int) -> tuple[bool, int]:
        """Check if *key* is within *limit* and record if allowed.

        Returns ``(allowed, retry_after_seconds)``. If allowed, retry_after is 0.
        """
        now = time.monotonic()
        self._cleanup_stale(now)
        cutoff = now - self.window

        if key not in self._buckets:
            self._buckets[key] = []

        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]

        if len(self._buckets[key]) >= limit:
            oldest = self._buckets[key][0]
            retry_after = int(oldest - cutoff) + 1
            return False, max(retry_after, 1)

        self._buckets[key].append(now)
        return True, 0
