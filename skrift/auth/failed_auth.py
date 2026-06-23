"""Per-IP failed-auth tracker.

A thin wrapper over a :class:`~skrift.lib.sliding_window.SlidingWindowCounter`
that records *only failed* attempts and blocks an IP once it crosses a
threshold within the window. The counter is supplied by the shared rate
limiter (:func:`skrift.ratelimit.get_limiter`), so it follows the same
Redis-vs-in-memory backend selection as every other limiter.
"""

from __future__ import annotations

from skrift.lib.sliding_window import InMemorySlidingWindowCounter, SlidingWindowCounter


class FailedAuthLimiter:
    """Per-IP sliding window that tracks failed auth attempts.

    Only records *failed* attempts; successful requests don't touch it.
    """

    def __init__(
        self,
        max_failures: int = 1,
        window: float = 60.0,
        counter: SlidingWindowCounter | None = None,
    ) -> None:
        self.max_failures = max_failures
        self._counter: SlidingWindowCounter = (
            counter or InMemorySlidingWindowCounter(window=window)
        )

    async def record_failure(self, ip: str) -> None:
        await self._counter.record(ip)

    async def is_blocked(self, ip: str) -> bool:
        return await self._counter.count(ip) >= self.max_failures
