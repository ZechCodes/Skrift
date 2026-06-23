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

    async def check_and_record_multi(
        self, key: str, limits: list[tuple[int, float]]
    ) -> tuple[bool, int]: ...

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
        # Multi-window keys hold all hit timestamps in a single list; each
        # window is evaluated as a count over a different sub-range, so they
        # can't share the single-window cleanup (which prunes by self.window).
        self._multi_buckets: dict[str, list[float]] = {}
        self._max_multi_window = 0.0
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

        multi_cutoff = now - self._max_multi_window
        stale_multi = []
        for key, timestamps in self._multi_buckets.items():
            self._multi_buckets[key] = [t for t in timestamps if t > multi_cutoff]
            if not self._multi_buckets[key]:
                stale_multi.append(key)
        for key in stale_multi:
            del self._multi_buckets[key]

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

    async def check_and_record_multi(
        self, key: str, limits: list[tuple[int, float]]
    ) -> tuple[bool, int]:
        """Check *key* against every ``(limit, window_seconds)`` pair.

        Denies if *any* window is exceeded; records a hit only when *every*
        window passes (all-or-nothing — a denied request consumes no slot).
        Returns ``(allowed, retry_after_seconds)`` where retry_after reflects
        the binding (longest-blocking) window. Atomic by virtue of running to
        completion without awaiting between the check and the record.
        """
        if not limits:
            return True, 0

        now = time.monotonic()
        max_window = max(window for _, window in limits)
        if max_window > self._max_multi_window:
            self._max_multi_window = max_window
        self._cleanup_stale(now)

        timestamps = [t for t in self._multi_buckets.get(key, []) if t > now - max_window]
        self._multi_buckets[key] = timestamps

        retry_after = 0
        for limit, window in limits:
            cutoff = now - window
            in_window = [t for t in timestamps if t > cutoff]
            if len(in_window) >= limit:
                # The (count - limit + 1)-th oldest entry must age out before a
                # new hit fits; that entry sits at index (count - limit).
                target = in_window[len(in_window) - limit]
                window_retry = int(target - cutoff) + 1
                retry_after = max(retry_after, window_retry)

        if retry_after:
            return False, max(retry_after, 1)

        timestamps.append(now)
        return True, 0
