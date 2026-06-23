"""Backend-aware rate limiting primitive.

This is the single place that decides whether rate limiting runs on Redis
(shared across replicas) or in-memory (process-local), so every limiter —
the built-in middleware, the failed-auth tracker, and any app code — agrees
on the backend. Set ``redis.url`` and they all go distributed; leave it unset
and they all stay per-process. No consumer constructs counters or reads
``settings.redis`` directly.

Two surfaces:

* :func:`get_limiter` → :class:`RateLimiter` for the common case::

      from skrift.ratelimit import get_limiter

      verdict = await get_limiter().check(
          name="capture",                       # namespace for keys
          key=client_ip,                        # caller identity
          limits=[(1, "minute"), (6, "hour")],  # one or more (limit, window)
      )
      if not verdict.allowed:
          raise TooManyRequests(retry_after=verdict.retry_after)

* :meth:`RateLimiter.get_counter` for the lower-level ``record``/``count`` or
  single-window ``check_and_record`` pattern (e.g. the failed-auth tracker).

The ``check`` call evaluates every ``(limit, window)`` pair, denies if any is
exceeded, records nothing on denial, and reports ``retry_after`` for the
binding (longest-blocking) window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from skrift.config import RedisConfig, get_settings, normalize_window
from skrift.lib.sliding_window import InMemorySlidingWindowCounter, SlidingWindowCounter

logger = logging.getLogger(__name__)

# normalize_window is re-exported from config so callers can import it from
# either module; config owns it to avoid an import cycle (config can't import
# this module).
__all__ = [
    "RateLimiter",
    "Verdict",
    "get_limiter",
    "set_limiter",
    "reset_limiter",
    "normalize_window",
]

# Redis key namespace for limiter counters: e.g. "skrift:ratelimit:<name>:<key>".
_KEY_NAMESPACE = ("skrift", "ratelimit")

# Nominal window for the per-name multi-window counter. The multi-window check
# carries its own windows, so this only affects the counter's single-window
# bookkeeping (unused on the ``check`` path).
_MULTI_COUNTER_WINDOW = 60.0


@dataclass(frozen=True)
class Verdict:
    """Outcome of a rate-limit check.

    ``retry_after`` is the number of seconds until the binding window frees up;
    it is 0 when ``allowed`` is True.
    """

    allowed: bool
    retry_after: int = 0


class RateLimiter:
    """Owns backend selection and reuses a single Redis client process-wide.

    Prefer :func:`get_limiter` over constructing this directly; the explicit
    constructor exists for wiring and tests.
    """

    def __init__(
        self,
        *,
        redis_client: object | None = None,
        redis_config: RedisConfig | None = None,
    ) -> None:
        self._redis_client = redis_client
        self._redis_config = redis_config or RedisConfig()
        self._counters: dict[tuple[str, float], SlidingWindowCounter] = {}
        self._multi_counters: dict[str, SlidingWindowCounter] = {}

    @classmethod
    def from_settings(cls, settings) -> "RateLimiter":
        """Build a limiter from app settings, choosing Redis vs in-memory."""
        redis_client = None
        if settings.redis.url:
            try:
                from redis.asyncio import from_url as redis_from_url

                redis_client = redis_from_url(settings.redis.url)
                logger.info("Rate limiter backend: Redis (%s)", settings.redis.url)
            except ImportError:
                logger.warning(
                    "redis.asyncio not available; falling back to in-memory rate limiter"
                )
        return cls(redis_client=redis_client, redis_config=settings.redis)

    @property
    def backend(self) -> str:
        return "redis" if self._redis_client is not None else "memory"

    @property
    def redis_client(self) -> object | None:
        """The shared Redis client, or None on the in-memory backend.

        Exposed so other Redis-backed features can reuse the one connection.
        """
        return self._redis_client

    def _make_counter(self, window: float, name: str) -> SlidingWindowCounter:
        if self._redis_client is not None:
            from skrift.lib.sliding_window_redis import RedisSlidingWindowCounter

            prefix = self._redis_config.make_key(*_KEY_NAMESPACE, name)
            return RedisSlidingWindowCounter(
                self._redis_client, window=window, prefix=prefix
            )
        return InMemorySlidingWindowCounter(window=window)

    def get_counter(self, window: float, name: str) -> SlidingWindowCounter:
        """Return a namespaced single-window counter, reused across calls.

        For callers that need the ``record``/``count`` or ``check_and_record``
        surface directly. ``window`` is in seconds.
        """
        cache_key = (name, window)
        counter = self._counters.get(cache_key)
        if counter is None:
            counter = self._counters[cache_key] = self._make_counter(window, name)
        return counter

    def _multi_counter(self, name: str) -> SlidingWindowCounter:
        # One stable counter per name, independent of the limits passed to
        # check(), so multi-window state for a name never splits.
        counter = self._multi_counters.get(name)
        if counter is None:
            counter = self._multi_counters[name] = self._make_counter(
                _MULTI_COUNTER_WINDOW, name
            )
        return counter

    async def check(
        self,
        name: str,
        key: str,
        limits: list[tuple[int, str | int | float]],
    ) -> Verdict:
        """Check ``key`` under ``name`` against one or more ``(limit, window)``.

        Denies if any window is exceeded; records a hit only when all windows
        pass (a denied request consumes no slot). ``window`` may be a friendly
        period name or seconds.
        """
        if not limits:
            return Verdict(allowed=True)
        normalized = [(limit, normalize_window(period)) for limit, period in limits]
        counter = self._multi_counter(name)
        allowed, retry_after = await counter.check_and_record_multi(key, normalized)
        return Verdict(allowed=allowed, retry_after=retry_after)

    async def aclose(self) -> None:
        """Close the shared Redis client, if any."""
        client = self._redis_client
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """Return the process-wide rate limiter, building it on first use."""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter.from_settings(get_settings())
    return _limiter


def set_limiter(limiter: RateLimiter) -> None:
    """Install a pre-built limiter as the process-wide singleton.

    Used by app startup so the limiter and its Redis client are created once
    and shared, rather than lazily rebuilt.
    """
    global _limiter
    _limiter = limiter


def reset_limiter() -> None:
    """Drop the cached limiter so the next ``get_limiter`` rebuilds it.

    Called when settings are reloaded (e.g. in tests). Does not close the
    Redis client — app shutdown owns that via :meth:`RateLimiter.aclose`.
    """
    global _limiter
    _limiter = None
