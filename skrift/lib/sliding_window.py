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


# Multi-window histories keep one timestamp per hit for windows up to this
# length and fall back to counters over fixed-width buckets beyond it. Long
# windows (hour, day) are where per-hit timestamps pile up, and they are also
# where a bucket's worth of imprecision is proportionally negligible.
COARSE_WINDOW_THRESHOLD = 900.0
COARSE_BUCKET_SECONDS = 60.0


class _MultiWindowHistory:
    """Hit history for one key on the multi-window path.

    Windows up to :data:`COARSE_WINDOW_THRESHOLD` are counted from exact
    per-hit timestamps. Longer windows are counted from
    :data:`COARSE_BUCKET_SECONDS`-wide buckets that hold a hit *count*
    instead of a timestamp per hit, so a key's long-window footprint grows
    with elapsed time (bounded by the limit itself, since hits stop being
    recorded once a limit is reached) rather than with request volume.

    The tradeoff: the bucket straddling a long window's trailing edge is
    counted in full even though part of it has aged out, so long windows may
    over-count by up to one bucket's worth of hits — at most a minute of
    traffic against an hour or a day. They never under-count, so a configured
    limit is never silently exceeded.

    Retention is per key: history is dropped once it falls outside the
    longest window *this* key has been checked against, so a day-long rule on
    one route can't pin the history of keys that only ever hit a minute-long
    rule.
    """

    __slots__ = (
        "_coarse_buckets",
        "_fine_horizon",
        "_hits",
        "_last_untracked_hit",
        "_retention",
    )

    def __init__(self) -> None:
        self._hits: list[float] = []
        self._coarse_buckets: list[tuple[float, int]] = []
        self._fine_horizon = 0.0
        self._retention = 0.0
        # Hits recorded before any short window was seen never reached _hits,
        # so an exact count over a window covering them would come up short.
        self._last_untracked_hit = float("-inf")

    @property
    def entry_count(self) -> int:
        """Retained entries — one per hit when fine, one per bucket when coarse."""
        return len(self._hits) + len(self._coarse_buckets)

    @property
    def hit_count(self) -> int:
        """Hits still retained. The two views overlap, so take the larger."""
        return max(len(self._hits), sum(count for _, count in self._coarse_buckets))

    @property
    def is_empty(self) -> bool:
        return not self._hits and not self._coarse_buckets

    def observe(self, windows: list[float]) -> None:
        """Widen retention to cover every window this key is checked against."""
        for window in windows:
            self._retention = max(self._retention, window)
            if window <= COARSE_WINDOW_THRESHOLD:
                self._fine_horizon = max(self._fine_horizon, window)

    def prune(self, now: float) -> None:
        """Drop history that no window of this key can still see."""
        fine_cutoff = now - self._fine_horizon
        if self._hits and self._hits[0] <= fine_cutoff:
            self._hits = [hit for hit in self._hits if hit > fine_cutoff]

        coarse_cutoff = now - self._retention
        buckets = self._coarse_buckets
        if buckets and buckets[0][0] + COARSE_BUCKET_SECONDS <= coarse_cutoff:
            self._coarse_buckets = [
                bucket
                for bucket in buckets
                if bucket[0] + COARSE_BUCKET_SECONDS > coarse_cutoff
            ]

    def record(self, now: float) -> None:
        """Record one hit at *now*."""
        if self._fine_horizon > 0:
            self._hits.append(now)
        else:
            self._last_untracked_hit = now

        if self._retention <= COARSE_WINDOW_THRESHOLD and self._fine_horizon > 0:
            # Every window this key uses is counted exactly; buckets would be
            # dead weight. A key whose windows later grow past the threshold
            # starts bucketing from that point on.
            return

        bucket_start = now - (now % COARSE_BUCKET_SECONDS)
        if self._coarse_buckets and self._coarse_buckets[-1][0] == bucket_start:
            start, count = self._coarse_buckets[-1]
            self._coarse_buckets[-1] = (start, count + 1)
        else:
            self._coarse_buckets.append((bucket_start, 1))

    def retry_after_for(self, limit: int, window: float, now: float) -> int:
        """Seconds until this key fits under *limit* for *window*; 0 if it does."""
        cutoff = now - window
        if limit <= 0:
            # A zero limit denies everything; no amount of aging out helps.
            return max(int(window) + 1, 1)

        if self._counts_exactly(window, now):
            in_window = [hit for hit in self._hits if hit > cutoff]
            if len(in_window) < limit:
                return 0
            # The (count - limit + 1)-th oldest hit must age out before a new
            # one fits; that hit sits at index (count - limit).
            deadline = in_window[len(in_window) - limit]
        else:
            in_window = [
                bucket
                for bucket in self._coarse_buckets
                if bucket[0] + COARSE_BUCKET_SECONDS > cutoff
            ]
            total = sum(count for _, count in in_window)
            if total < limit:
                return 0
            # Buckets age out whole, so wait for the bucket holding the
            # (total - limit + 1)-th oldest hit to leave the window entirely.
            excess = total - limit + 1
            shed = 0
            deadline = in_window[-1][0] + COARSE_BUCKET_SECONDS
            for start, count in in_window:
                shed += count
                if shed >= excess:
                    deadline = start + COARSE_BUCKET_SECONDS
                    break

        return max(int(deadline - cutoff) + 1, 1)

    def _counts_exactly(self, window: float, now: float) -> bool:
        """Whether ``_hits`` holds every hit *window* can see."""
        return (
            window <= COARSE_WINDOW_THRESHOLD
            and self._last_untracked_hit <= now - window
        )


class InMemorySlidingWindowCounter:
    """Process-local sliding window counter.

    Tracks per-key hit counts within a sliding time window. Periodically
    prunes stale entries to bound memory usage.
    """

    def __init__(self, window: float = 60.0, cleanup_interval: float = 60.0) -> None:
        self.window = window
        self._buckets: dict[str, list[float]] = {}
        # Multi-window keys evaluate several windows over one history, so they
        # can't share the single-window cleanup (which prunes by self.window);
        # each history prunes itself against the windows its key uses.
        self._multi_buckets: dict[str, _MultiWindowHistory] = {}
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

        stale_multi = []
        for key, history in self._multi_buckets.items():
            history.prune(now)
            if history.is_empty:
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

        Windows longer than :data:`COARSE_WINDOW_THRESHOLD` are counted from
        coarse buckets rather than per-hit timestamps; see
        :class:`_MultiWindowHistory` for the memory/precision tradeoff.
        """
        if not limits:
            return True, 0

        now = time.monotonic()
        self._cleanup_stale(now)

        history = self._multi_buckets.get(key)
        if history is None:
            history = self._multi_buckets[key] = _MultiWindowHistory()
        history.observe([window for _, window in limits])
        history.prune(now)

        retry_after = 0
        for limit, window in limits:
            retry_after = max(retry_after, history.retry_after_for(limit, window, now))

        if retry_after:
            if history.is_empty:
                # Nothing was ever recorded for this key (a zero limit denies
                # on sight); don't leave an empty history behind.
                del self._multi_buckets[key]
            return False, max(retry_after, 1)

        history.record(now)
        return True, 0
