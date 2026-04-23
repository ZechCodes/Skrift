"""Redis-backed sliding window counter.

Each bucket is a sorted set keyed as ``{prefix}:{key}`` where entries are
request timestamps (milliseconds since the epoch). A single Lua script
runs the prune-count-insert cycle atomically, so two replicas racing on
the same bucket can't both "see limit-1, insert, end up at limit+1".

Keeps the same ``(allowed, retry_after_seconds)`` return shape as the
in-memory counter so it's a drop-in swap.
"""

from __future__ import annotations

import itertools
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis


_record_seq = itertools.count()


# Lua script: atomic sliding-window check-and-record.
#
# KEYS[1] = sorted set key
# ARGV[1] = window size in ms
# ARGV[2] = limit
# ARGV[3] = now (ms since epoch)
# ARGV[4] = unique member tag (caller supplies a unique string per call)
#
# Returns { allowed, retry_after_ms } as an array of two integers.
_CHECK_AND_RECORD_SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now - window

redis.call('ZREMRANGEBYSCORE', key, 0, cutoff)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_score = tonumber(oldest[2])
    local retry_ms = (oldest_score + window) - now
    if retry_ms < 1 then
        retry_ms = 1
    end
    return {0, retry_ms}
end

redis.call('ZADD', key, now, member)
-- Expire slightly after the window so idle keys disappear.
redis.call('PEXPIRE', key, window + 1000)
return {1, 0}
"""


class RedisSlidingWindowCounter:
    """Redis-backed counterpart of :class:`InMemorySlidingWindowCounter`.

    Args:
        redis: ``redis.asyncio.Redis`` client instance.
        window: Window size in seconds.
        prefix: Namespace prefix for keys, e.g. ``"skrift:ratelimit"``.
    """

    def __init__(self, redis: "Redis", *, window: float = 60.0, prefix: str = "skrift:ratelimit") -> None:
        self._redis = redis
        self.window = window
        self._window_ms = int(window * 1000)
        self._prefix = prefix.rstrip(":")
        self._script_sha: str | None = None

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def _eval(
        self, *, key: str, limit: int, now_ms: int, member: str
    ) -> tuple[int, int]:
        # Lazy-load the Lua script; fall back to EVAL on NOSCRIPT errors.
        if self._script_sha is None:
            self._script_sha = await self._redis.script_load(_CHECK_AND_RECORD_SCRIPT)
        try:
            result = await self._redis.evalsha(
                self._script_sha,
                1,
                self._key(key),
                self._window_ms,
                limit,
                now_ms,
                member,
            )
        except Exception as exc:  # noqa: BLE001
            if "NOSCRIPT" not in str(exc).upper():
                raise
            self._script_sha = await self._redis.script_load(_CHECK_AND_RECORD_SCRIPT)
            result = await self._redis.evalsha(
                self._script_sha,
                1,
                self._key(key),
                self._window_ms,
                limit,
                now_ms,
                member,
            )
        allowed, retry_ms = int(result[0]), int(result[1])
        return allowed, retry_ms

    async def check_and_record(self, key: str, limit: int) -> tuple[bool, int]:
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}-{next(_record_seq)}-{uuid.uuid4().hex[:8]}"
        allowed, retry_ms = await self._eval(
            key=key, limit=limit, now_ms=now_ms, member=member
        )
        if allowed:
            return True, 0
        retry_seconds = max(1, (retry_ms + 999) // 1000)  # round up to whole seconds
        return False, retry_seconds

    async def record(self, key: str) -> None:
        # Record a hit unconditionally.
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - self._window_ms
        # Unique per-record member so two calls within the same millisecond
        # both count (sorted-set members are unique by value, not score).
        member = f"{now_ms}-{next(_record_seq)}-{uuid.uuid4().hex[:8]}"
        k = self._key(key)
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(k, 0, cutoff)
        pipe.zadd(k, {member: now_ms})
        pipe.pexpire(k, self._window_ms + 1000)
        await pipe.execute()

    async def count(self, key: str) -> int:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - self._window_ms
        k = self._key(key)
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(k, 0, cutoff)
        pipe.zcard(k)
        results = await pipe.execute()
        return int(results[-1])
