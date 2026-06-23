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


# Lua script: atomic multi-window check-and-record.
#
# A single sorted set holds every hit timestamp for the key; each window is
# just a count over a different time sub-range. The script denies if ANY
# window is at/over its limit and only ZADDs when EVERY window passes — so a
# denied request records nothing.
#
# KEYS[1] = sorted set key
# ARGV[1] = now (ms since epoch)
# ARGV[2] = max window in ms (for pruning + key expiry)
# ARGV[3] = unique member tag
# ARGV[4..] = flattened (window_ms, limit) pairs
#
# Returns { allowed, retry_after_ms } where retry_after_ms is the max across
# all exceeded windows (the binding, longest-blocking window).
_CHECK_AND_RECORD_MULTI_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_window = tonumber(ARGV[2])
local member = ARGV[3]

redis.call('ZREMRANGEBYSCORE', key, 0, now - max_window)

local blocked = 0
local max_retry = 0
local i = 4
while i <= #ARGV do
    local window = tonumber(ARGV[i])
    local limit = tonumber(ARGV[i + 1])
    local cutoff = now - window
    local count = redis.call('ZCOUNT', key, '(' .. cutoff, '+inf')
    if count >= limit then
        local idx = count - limit
        local entry = redis.call(
            'ZRANGEBYSCORE', key, '(' .. cutoff, '+inf', 'LIMIT', idx, 1, 'WITHSCORES'
        )
        local score = tonumber(entry[2])
        local retry = (score + window) - now
        if retry < 1 then
            retry = 1
        end
        if retry > max_retry then
            max_retry = retry
        end
        blocked = 1
    end
    i = i + 2
end

if blocked == 1 then
    return {0, max_retry}
end

redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, max_window + 1000)
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
        # Cache the loaded SHA per script source, so each Lua script is loaded
        # once and reused across calls.
        self._script_shas: dict[str, str] = {}

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    @staticmethod
    def _member(now_ms: int) -> str:
        # Unique per call so two hits in the same millisecond both count
        # (sorted-set members are unique by value, not score).
        return f"{now_ms}-{next(_record_seq)}-{uuid.uuid4().hex[:8]}"

    async def _run_script(self, script: str, key: str, *args) -> list:
        """Run a cached Lua script, reloading once on NOSCRIPT."""
        sha = self._script_shas.get(script)
        if sha is None:
            sha = self._script_shas[script] = await self._redis.script_load(script)
        try:
            return await self._redis.evalsha(sha, 1, self._key(key), *args)
        except Exception as exc:  # noqa: BLE001
            if "NOSCRIPT" not in str(exc).upper():
                raise
            sha = self._script_shas[script] = await self._redis.script_load(script)
            return await self._redis.evalsha(sha, 1, self._key(key), *args)

    @staticmethod
    def _retry_seconds(retry_ms: int) -> int:
        return max(1, (retry_ms + 999) // 1000)  # round up to whole seconds

    async def check_and_record(self, key: str, limit: int) -> tuple[bool, int]:
        now_ms = int(time.time() * 1000)
        result = await self._run_script(
            _CHECK_AND_RECORD_SCRIPT,
            key,
            self._window_ms,
            limit,
            now_ms,
            self._member(now_ms),
        )
        if int(result[0]):
            return True, 0
        return False, self._retry_seconds(int(result[1]))

    async def check_and_record_multi(
        self, key: str, limits: list[tuple[int, float]]
    ) -> tuple[bool, int]:
        if not limits:
            return True, 0
        now_ms = int(time.time() * 1000)
        max_window_ms = int(max(window for _, window in limits) * 1000)
        window_args: list[int] = []
        for limit, window in limits:
            window_args.extend((int(window * 1000), limit))
        result = await self._run_script(
            _CHECK_AND_RECORD_MULTI_SCRIPT,
            key,
            now_ms,
            max_window_ms,
            self._member(now_ms),
            *window_args,
        )
        if int(result[0]):
            return True, 0
        return False, self._retry_seconds(int(result[1]))

    async def record(self, key: str) -> None:
        # Record a hit unconditionally.
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - self._window_ms
        member = self._member(now_ms)
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
