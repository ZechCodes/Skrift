"""Redis-backed worker hot-path backends."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from skrift.workers.interfaces import TTL, BackendCapabilities, UpdateFn, resolve_ttl
from skrift.workers.models import (
    ClaimedJob,
    EventIdConflict,
    JobEnvelope,
    JobIdConflict,
    JobState,
    JobStatus,
    QueueStats,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Every queue write runs as a script that re-checks, atomically, the state it
# was decided on. `_queue_lock` expires after 10 s, so it cannot by itself stop
# a stalled worker or reaper from writing over a claim made after its lock ran
# out.

# A job's claim generation counts its claims; each claim increments it and
# takes the new value as its order. It is stored in the job's own envelope
# value, as a leading "_claim_generation" member that JobEnvelope ignores, so
# it is only ever lost together with the job. Every script that replaces the
# envelope carries it over.
_GENERATION_LUA = """
local function generation(stored)
    return tonumber(stored and string.match(stored, '^{"_claim_generation":(%d+),')) or 0
end
local function with_generation(envelope, n)
    local rest = string.match(envelope, '^{"_claim_generation":%d+,(.*)$')
    return '{"_claim_generation":' .. n .. ',' .. (rest or string.sub(envelope, 2))
end
"""

# KEYS: ready, claimed, claim, job
# ARGV: job_id, ready cutoff score, lease expiry score, queue, token, lease expiry iso,
#       the job's visibility timeout as read ("" if unset; tonumber of it and of a
#       JSON null are both nil)
# Returns the stored envelope and the claim's order, or nil if the job is no
# longer ready and unclaimed or its visibility timeout (which the lease was
# computed from) has changed.
_CLAIM_SCRIPT = _GENERATION_LUA + """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) then return false end
if redis.call('EXISTS', KEYS[3]) == 1 then return false end
local job = redis.call('GET', KEYS[4])
if not job then return false end
if tonumber(cjson.decode(job).visibility_timeout) ~= tonumber(ARGV[7]) then return false end
local claim_order = generation(job) + 1
redis.call('SET', KEYS[4], with_generation(job, claim_order))
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
redis.call('HSET', KEYS[3], 'queue', ARGV[4], 'token', ARGV[5], 'expires_at', ARGV[6])
return {job, claim_order}
"""

# KEYS: claim, job, ready, claimed, dead, dead_at, jobs_by_queue
# ARGV: job_id
_CANCEL_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 0 then return 0 end
redis.call('DEL', KEYS[2])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
redis.call('SREM', KEYS[5], ARGV[1])
redis.call('ZREM', KEYS[6], ARGV[1])
redis.call('SREM', KEYS[7], ARGV[1])
return 1
"""

# KEYS: job, claim, ready, claimed, dead
# ARGV: envelope as read, job_id, new envelope, ready score
# Returns -1 if the job is dead-lettered, 0 if its envelope changed, 2 if it is
# claimed: the wake is then recorded on the claim for its worker's nack to
# apply (the latest one wins; ack or a lost claim drops it).
_WAKE_SCRIPT = _GENERATION_LUA + """
if redis.call('SISMEMBER', KEYS[5], ARGV[2]) == 1 then return -1 end
if redis.call('EXISTS', KEYS[2]) == 1 then
    redis.call('HSET', KEYS[2], 'wake_at', ARGV[4])
    return 2
end
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], with_generation(ARGV[3], generation(ARGV[1])))
redis.call('ZREM', KEYS[4], ARGV[2])
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[2])
return 1
"""

# KEYS: job, ready, claimed, dead, dead_at, jobs_by_queue
# ARGV: job_id
# Drops the indexes of a job whose envelope is gone, unless it has been
# resubmitted since.
_FORGET_MISSING_JOB_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('SREM', KEYS[4], ARGV[1])
redis.call('ZREM', KEYS[5], ARGV[1])
redis.call('SREM', KEYS[6], ARGV[1])
return 1
"""

# KEYS: job, queue_names, jobs_by_queue, ready
# ARGV: envelope, queue, job_id, ready score
# Returns nil once the job is written, or the envelope already stored under
# this id so the caller can apply the idempotency/conflict rule to it.
_SUBMIT_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then return existing end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('SADD', KEYS[3], ARGV[3])
redis.call('ZADD', KEYS[4], ARGV[4], ARGV[3])
return false
"""

# KEYS: dead_at, dead, claim, job, ready, claimed, jobs_by_queue
# ARGV: job_id, cutoff score
# Deletes a dead-lettered job only while it is still dead-lettered no later
# than the cutoff and unclaimed, so a job resubmitted under the same id is
# never pruned.
_PRUNE_DEAD_JOB_SCRIPT = """
local dead_at = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not dead_at or tonumber(dead_at) > tonumber(ARGV[2]) then return 0 end
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
redis.call('DEL', KEYS[4])
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[5], ARGV[1])
redis.call('ZREM', KEYS[6], ARGV[1])
redis.call('SREM', KEYS[7], ARGV[1])
return 1
"""

_WAKE_ATTEMPTS = 5

# KEYS: claim, job, ready, claimed, dead, dead_at, jobs_by_queue
# ARGV: queue, token, job_id
_ACK_SCRIPT = """
local claim = redis.call('HMGET', KEYS[1], 'queue', 'token')
if claim[1] ~= ARGV[1] or claim[2] ~= ARGV[2] then return 0 end
redis.call('DEL', KEYS[2], KEYS[1])
redis.call('ZREM', KEYS[3], ARGV[3])
redis.call('ZREM', KEYS[4], ARGV[3])
redis.call('SREM', KEYS[5], ARGV[3])
redis.call('ZREM', KEYS[6], ARGV[3])
redis.call('SREM', KEYS[7], ARGV[3])
return 1
"""

# KEYS: claim, job, ready, claimed, dead, dead_at
# ARGV: queue, token, job_id, job_json, dead_letter ("1"/"0"), score,
#       job_json without ready_since
# A wake recorded on the claim (see _WAKE_SCRIPT) replaces the retry score; the
# envelope then carries no ready_since, which stats derives from that score.
_NACK_SCRIPT = _GENERATION_LUA + """
local claim = redis.call('HMGET', KEYS[1], 'queue', 'token')
if claim[1] ~= ARGV[1] or claim[2] ~= ARGV[2] then return 0 end
local pending_wake = redis.call('HGET', KEYS[1], 'wake_at')
local claims = generation(redis.call('GET', KEYS[2]))
if pending_wake and ARGV[5] ~= '1' then
    redis.call('SET', KEYS[2], with_generation(ARGV[7], claims))
else
    redis.call('SET', KEYS[2], with_generation(ARGV[4], claims))
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[4], ARGV[3])
if ARGV[5] == '1' then
    redis.call('ZREM', KEYS[3], ARGV[3])
    redis.call('SADD', KEYS[5], ARGV[3])
    redis.call('ZADD', KEYS[6], ARGV[6], ARGV[3])
else
    redis.call('ZADD', KEYS[3], pending_wake or ARGV[6], ARGV[3])
end
return 1
"""

# KEYS: claim, job, ready, claimed
# ARGV: token read by the reaper ("" if none), job_id, job_json, lease cutoff, ready score
_RELEASE_SCRIPT = _GENERATION_LUA + """
if (redis.call('HGET', KEYS[1], 'token') or '') ~= ARGV[1] then return 0 end
local expires = redis.call('ZSCORE', KEYS[4], ARGV[2])
if not expires or tonumber(expires) > tonumber(ARGV[4]) then return 0 end
redis.call('SET', KEYS[2], with_generation(ARGV[3], generation(redis.call('GET', KEYS[2]))))
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[4], ARGV[2])
redis.call('ZADD', KEYS[3], ARGV[5], ARGV[2])
return 1
"""

# A state update's write, which lands only if the update still holds the key's
# lock: an update that stalls past the lock's expiry must not overwrite one
# made since (#203). It writes the value, its TTL and its index entries, and
# releases the lock, all at once.
# KEYS: value, lock, state keys, worker job index, active worker jobs
# ARGV: lock token, value, TTL in ms ("" for none), state key,
#       "active"/"inactive" for a worker job state ("" for any other value),
#       worker job index score
_UPDATE_SCRIPT = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
if ARGV[3] == '' then
    redis.call('SET', KEYS[1], ARGV[2])
else
    redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
end
redis.call('SADD', KEYS[3], ARGV[4])
if ARGV[5] ~= '' then
    redis.call('ZADD', KEYS[4], ARGV[6], ARGV[4])
    if ARGV[5] == 'active' then
        redis.call('SADD', KEYS[5], ARGV[4])
    else
        redis.call('SREM', KEYS[5], ARGV[4])
    end
end
redis.call('DEL', KEYS[2])
return 1
"""


# Deletes a state value if this process still holds its lock, and drops it from
# the indexes, all in one step, then releases the lock. Returns 1 if it deleted.
# KEYS: value, lock, state keys set, worker job index, active worker job set
# ARGV: lock token, state key
_DELETE_SCRIPT = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[3], ARGV[2])
redis.call('ZREM', KEYS[4], ARGV[2])
redis.call('SREM', KEYS[5], ARGV[2])
redis.call('DEL', KEYS[2])
return 1
"""


# Drops a state key from indexes if its value is still absent, so a concurrent
# update that has since written it keeps its entries (#217). Returns 1 if it did.
# KEYS: value, then ARGV[2] sets and the rest sorted sets to drop the key from
# ARGV: state key, number of sets
_FORGET_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
for i = 2, #KEYS do
    if i - 1 <= tonumber(ARGV[2]) then
        redis.call('SREM', KEYS[i], ARGV[1])
    else
        redis.call('ZREM', KEYS[i], ARGV[1])
    end
end
return 1
"""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _score(value: datetime) -> float:
    return _utc(value).timestamp()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode()
    return json.loads(value)


def _value_to_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return {
            "__skrift_pydantic__": f"{value.__class__.__module__}:{value.__class__.__name__}",
            "value": value.model_dump(mode="json"),
        }
    return value


def _value_from_json(value: Any) -> Any:
    if not (
        isinstance(value, dict)
        and "__skrift_pydantic__" in value
        and "value" in value
    ):
        return value
    module_path, class_name = value["__skrift_pydantic__"].split(":", 1)
    cls = getattr(importlib.import_module(module_path), class_name)
    return cls.model_validate(value["value"])


def _job_to_json(job: JobEnvelope) -> str:
    return _json_dumps(job.model_dump(mode="json"))


def _job_from_json(value: Any) -> JobEnvelope:
    return JobEnvelope.model_validate(_json_loads(value))


class _RedisBackend:
    def __init__(
        self,
        *,
        client: Any | None = None,
        settings: Any | None = None,
        prefix: str | None = None,
        **_: Any,
    ) -> None:
        self._client = client
        self._owns_client = False
        self._prefix = prefix
        redis_url = os.environ.get("SKRIFT_WORKERS_REDIS_URL")
        if settings is not None:
            redis_config = getattr(settings, "redis", None)
            if self._prefix is None and redis_config is not None:
                self._prefix = redis_config.make_key("skrift", "workers")
            if redis_url is None and redis_config is not None:
                redis_url = redis_config.url
        if self._client is None and redis_url:
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:  # pragma: no cover - dependency guidance
                raise RuntimeError(
                    "Redis worker backends require the redis package. "
                    "Install with: pip install 'skrift[redis]'"
                ) from exc
            self._client = aioredis.Redis.from_url(redis_url)
            self._owns_client = True
        self._prefix = self._prefix or "skrift:workers"
        if self._client is None:
            raise ValueError(
                "Redis worker backend configured without redis.url. "
                "Set redis.url or pass a Redis client."
            )

    def _key(self, *parts: str) -> str:
        return ":".join([self._prefix, *parts])

    async def close(self) -> None:
        if self._owns_client and hasattr(self._client, "aclose"):
            await self._client.aclose()


class RedisStateStore(_RedisBackend):
    """Redis key/value state store with TTL and atomic update support."""

    capabilities = BackendCapabilities({"ttl", "atomic_update", "prefix_scan"})

    async def get(self, key: str) -> Any:
        return _value_from_json(_json_loads(await self._client.get(self._state_key(key))))

    async def set(self, key: str, value: Any, *, ttl: TTL = None) -> None:
        redis_key = self._state_key(key)
        payload = _json_dumps(_value_to_json(value))
        resolved_ttl = resolve_ttl(ttl, value)
        if resolved_ttl is None:
            await self._client.set(redis_key, payload)
        else:
            await self._client.set(redis_key, payload, px=max(1, int(resolved_ttl * 1000)))
        await self._client.sadd(self._key("state", "keys"), key)
        if key.startswith("workers:jobs:"):
            await self._index_worker_job_state(key, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(self._state_key(key))
        await self._client.srem(self._key("state", "keys"), key)
        if key.startswith("workers:jobs:"):
            await self._client.zrem(self._key("state", "worker_jobs"), key)
            await self._client.srem(self._key("state", "worker_jobs_active"), key)

    async def update(self, key: str, fn: UpdateFn, *, ttl: TTL = None) -> Any:
        """Replace ``key`` with ``fn`` of its value, under the key's lock.

        Raises ``LockNotOwnedError``, having written nothing, if the lock
        expired while ``fn`` ran and so another update may have written since.
        ``fn`` is not run again.
        """
        from redis.exceptions import LockNotOwnedError

        lock = self._client.lock(self._key("state", "locks", key), timeout=10)
        await lock.acquire()
        written = False
        try:
            current = await self.get(key)
            next_value = fn(current)
            if inspect.isawaitable(next_value):
                next_value = await next_value
            # The TTL is resolved against next_value, so terminal TTL is
            # applied based on the post-`fn` state.
            resolved_ttl = resolve_ttl(ttl, next_value)
            index = self._worker_job_index(key, next_value)
            written = bool(
                await self._client.eval(
                    _UPDATE_SCRIPT,
                    5,
                    self._state_key(key),
                    lock.name,
                    self._key("state", "keys"),
                    self._key("state", "worker_jobs"),
                    self._key("state", "worker_jobs_active"),
                    lock.local.token,
                    _json_dumps(_value_to_json(next_value)),
                    "" if resolved_ttl is None else max(1, int(resolved_ttl * 1000)),
                    key,
                    *(index or ("", 0)),
                )
            )
        finally:
            if not written:
                # Token-checked, so a lock another update has taken is left alone.
                try:
                    await lock.release()
                except LockNotOwnedError:
                    pass
        if not written:
            raise LockNotOwnedError(
                f"State update of {key!r} not written: its lock expired before the write"
            )
        return next_value

    async def keys(self, prefix: str = "") -> list[str]:
        known = await self._client.smembers(self._key("state", "keys"))
        keys: list[str] = []
        stale: list[str] = []
        for raw in known:
            key = raw.decode() if isinstance(raw, bytes) else str(raw)
            if not await self._client.exists(self._state_key(key)):
                stale.append(key)
                continue
            if key.startswith(prefix):
                keys.append(key)
        for key in stale:
            await self._forget(key, sets=(self._key("state", "keys"),))
        return sorted(keys)

    async def worker_job_states(self, *, limit: int | None = None) -> tuple[list[JobState], int]:
        job_index = self._key("state", "worker_jobs")
        total = int(await self._client.zcard(job_index))
        end = -1 if limit is None else limit - 1
        keys = await self._client.zrevrange(job_index, 0, end)
        states: list[JobState] = []
        stale: list[str] = []
        for raw in keys:
            key = raw.decode() if isinstance(raw, bytes) else str(raw)
            state = await self.get(key)
            if state is None:
                stale.append(key)
            elif isinstance(state, JobState):
                states.append(state)
        for key in stale:
            if await self._forget(
                key, sets=(self._key("state", "worker_jobs_active"),), sorted_sets=(job_index,)
            ):
                total = max(0, total - 1)
        return states, total

    async def worker_job_counts(self) -> dict[str, int]:
        return {
            "total": int(await self._client.zcard(self._key("state", "worker_jobs"))),
            "active": int(await self._client.scard(self._key("state", "worker_jobs_active"))),
        }

    async def prune_terminal_job_states(self, *, max_age_seconds: float) -> int:
        cutoff = _score(_now() - timedelta(seconds=max_age_seconds))
        job_index = self._key("state", "worker_jobs")
        keys = await self._client.zrangebyscore(job_index, "-inf", cutoff)
        terminal = {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCELLED,
        }
        count = 0
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            state = await self.get(key)
            if state is None:
                await self._forget(
                    key,
                    sets=(self._key("state", "worker_jobs_active"),),
                    sorted_sets=(job_index,),
                )
                continue
            if isinstance(state, JobState) and state.status in terminal:
                count += await self._delete_if_prunable(key, terminal, cutoff)
        return count

    async def _forget(
        self, key: str, *, sets: tuple[str, ...], sorted_sets: tuple[str, ...] = ()
    ) -> bool:
        """Drop ``key`` from index ``sets`` and ``sorted_sets`` if its value is
        still absent, in one step. Returns whether it did."""
        return bool(
            await self._client.eval(
                _FORGET_SCRIPT,
                1 + len(sets) + len(sorted_sets),
                self._state_key(key),
                *sets,
                *sorted_sets,
                key,
                len(sets),
            )
        )

    async def _delete_if_prunable(
        self, key: str, terminal: set[JobStatus], cutoff: float
    ) -> int:
        """Delete a job state under the lock ``update`` takes, if it is still
        terminal and older than ``cutoff`` then, so a concurrent update is
        neither lost nor undone (#217).

        Returns 1 if it deleted, and 0 if the state had changed or the lock
        expired before the delete.
        """
        from redis.exceptions import LockNotOwnedError

        lock = self._client.lock(self._key("state", "locks", key), timeout=10)
        await lock.acquire()
        deleted = False
        try:
            state = await self.get(key)
            if (
                isinstance(state, JobState)
                and state.status in terminal
                and _score(state.updated_at) <= cutoff
            ):
                deleted = bool(
                    await self._client.eval(
                        _DELETE_SCRIPT,
                        5,
                        self._state_key(key),
                        lock.name,
                        self._key("state", "keys"),
                        self._key("state", "worker_jobs"),
                        self._key("state", "worker_jobs_active"),
                        lock.local.token,
                        key,
                    )
                )
        finally:
            if not deleted:
                try:
                    await lock.release()
                except LockNotOwnedError:
                    pass
        return int(deleted)

    def _state_key(self, key: str) -> str:
        return self._key("state", "values", key)

    @staticmethod
    def _worker_job_index(key: str, value: Any) -> tuple[str, float] | None:
        """Whether a worker job state is active, and its index score."""
        if not key.startswith("workers:jobs:"):
            return None
        if not isinstance(value, JobState):
            value = _value_from_json(value)
        if not isinstance(value, JobState):
            return None
        active = value.status in {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.PAUSED}
        return ("active" if active else "inactive"), _score(value.updated_at)

    async def _index_worker_job_state(self, key: str, value: Any) -> None:
        index = self._worker_job_index(key, value)
        if index is None:
            return
        status, score = index
        await self._client.zadd(self._key("state", "worker_jobs"), {key: score})
        active_key = self._key("state", "worker_jobs_active")
        if status == "active":
            await self._client.sadd(active_key, key)
        else:
            await self._client.srem(active_key, key)


class RedisEventLog(_RedisBackend):
    """Redis Streams event log backend."""

    capabilities = BackendCapabilities({"replay", "live_tail", "delete"})

    async def append(self, stream: str, event: dict[str, Any]) -> int:
        event_id = event.get("event_id")
        if event_id is not None:
            index_key = self._event_id_key(stream)
            existing_position = await self._client.hget(index_key, str(event_id))
            if existing_position is not None:
                existing_position_int = int(
                    existing_position.decode()
                    if isinstance(existing_position, bytes)
                    else existing_position
                )
                existing = await self.read(stream, from_position=existing_position_int, limit=1)
                if existing and existing[0][1] == event:
                    return existing_position_int
                raise EventIdConflict(
                    f"event_id {event_id!r} already exists in stream {stream!r}"
                )
        position = int(await self._client.incr(self._position_key(stream))) - 1
        fields = {
            "position": str(position),
            "event": _json_dumps(event),
        }
        job_id = event.get("job_id")
        if job_id is not None:
            fields["job_id"] = str(job_id)
        entry_id = await self._client.xadd(self._stream_key(stream), fields)
        await self._client.hset(self._id_key(stream), position, entry_id)
        if event_id is not None:
            await self._client.hset(self._event_id_key(stream), str(event_id), position)
        if job_id is not None:
            await self._client.zadd(
                self._filter_key(stream, "job_id", str(job_id)),
                {entry_id: position},
            )
        return position

    async def read(
        self, stream: str, *, from_position: int = 0, limit: int | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        min_id = "-"
        if from_position > 0:
            min_id = await self._entry_id_for_position(stream, from_position)
            if min_id is None:
                return []
        kwargs = {} if limit is None else {"count": limit}
        rows = await self._client.xrange(self._stream_key(stream), min_id, "+", **kwargs)
        return [
            (position, event)
            for position, event in (self._decode_event(row) for row in rows)
            if position >= from_position
        ]

    async def read_filtered(
        self,
        stream: str,
        *,
        filters: dict[str, Any],
        from_position: int = 0,
        limit: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        if set(filters) == {"job_id"}:
            kwargs = {} if limit is None else {"start": 0, "num": limit}
            ids = await self._client.zrangebyscore(
                self._filter_key(stream, "job_id", str(filters["job_id"])),
                from_position,
                "+inf",
                **kwargs,
            )
            events: list[tuple[int, dict[str, Any]]] = []
            for entry_id in ids:
                rows = await self._client.xrange(self._stream_key(stream), entry_id, entry_id)
                if rows:
                    events.append(self._decode_event(rows[0]))
            return events

        rows = await self.read(stream, from_position=from_position)
        matches = [
            (position, event)
            for position, event in rows
            if all(event.get(key) == value for key, value in filters.items())
        ]
        return matches if limit is None else matches[:limit]

    async def read_tail(self, stream: str, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
        if limit <= 0:
            return []
        rows = await self._client.xrevrange(self._stream_key(stream), "+", "-", count=limit)
        return list(reversed([self._decode_event(row) for row in rows]))

    async def subscribe(
        self, stream: str, *, from_position: int | None = None
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        cursor = 0
        if from_position is None:
            cursor = int(await self._client.get(self._position_key(stream)) or 0)
        else:
            cursor = from_position
        while True:
            events = await self.read(stream, from_position=cursor, limit=50)
            if not events:
                await asyncio.sleep(0.1)
                continue
            for position, event in events:
                cursor = position + 1
                yield position, event

    async def delete(self, stream: str) -> None:
        prefix = self._key("events", stream)
        async for key in self._client.scan_iter(f"{prefix}*"):
            await self._client.delete(key)

    async def list_streams(self, prefix: str = "") -> list[str]:
        base = f"{self._key('events')}:"
        suffix = ":stream"
        pattern = f"{base}{prefix}*{suffix}"
        streams: set[str] = set()
        async for key in self._client.scan_iter(pattern):
            if isinstance(key, bytes):
                key = key.decode()
            if key.startswith(base) and key.endswith(suffix):
                streams.add(key[len(base):-len(suffix)])
        return sorted(streams)

    async def prune_archived_events(
        self,
        stream: str,
        *,
        archived_position: int,
        max_age_seconds: float | None = None,
        max_entries: int | None = None,
    ) -> int:
        if archived_position <= 0:
            return 0
        max_id = await self._entry_id_for_position(stream, archived_position - 1)
        if max_id is None:
            return 0
        cutoff_ms = None
        if max_age_seconds is not None:
            cutoff_ms = int((_now() - timedelta(seconds=max_age_seconds)).timestamp() * 1000)
        stream_length = int(await self._client.xlen(self._stream_key(stream)))
        length_cutoff = max(0, stream_length - max_entries) if max_entries is not None else 0

        deleted = 0
        min_id = "-"
        page_size = 1000
        scanned_index = 0
        while True:
            rows = await self._client.xrange(
                self._stream_key(stream),
                min_id,
                max_id,
                count=page_size,
            )
            if not rows:
                break
            delete_ids: list[Any] = []
            last_entry_id: Any | None = None
            for row in rows:
                entry_id, _ = row
                last_entry_id = entry_id
                position, event = self._decode_event(row)
                stream_index = scanned_index
                scanned_index += 1
                if position >= archived_position:
                    continue
                old_enough = (
                    cutoff_ms is not None
                    and self._entry_timestamp_ms(entry_id) <= cutoff_ms
                )
                over_length = stream_index < length_cutoff
                if not old_enough and not over_length:
                    continue
                delete_ids.append(entry_id)
                await self._client.hdel(self._id_key(stream), str(position))
                job_id = event.get("job_id")
                if job_id is not None:
                    await self._client.zrem(
                        self._filter_key(stream, "job_id", str(job_id)),
                        entry_id,
                    )
            if delete_ids:
                await self._client.xdel(self._stream_key(stream), *delete_ids)
                deleted += len(delete_ids)
            if len(rows) < page_size or last_entry_id == max_id:
                break
            min_id = self._next_stream_id(last_entry_id)
        return deleted

    def _stream_key(self, stream: str) -> str:
        return self._key("events", stream, "stream")

    def _position_key(self, stream: str) -> str:
        return self._key("events", stream, "position")

    def _id_key(self, stream: str) -> str:
        return self._key("events", stream, "ids")

    def _event_id_key(self, stream: str) -> str:
        return self._key("events", stream, "event_ids")

    def _filter_key(self, stream: str, field: str, value: str) -> str:
        return self._key("events", stream, "filters", field, value)

    async def _entry_id_for_position(self, stream: str, position: int) -> str | None:
        entry_id = await self._client.hget(self._id_key(stream), str(position))
        if isinstance(entry_id, bytes):
            return entry_id.decode()
        return entry_id

    @staticmethod
    def _decode_event(row: tuple[Any, dict[Any, Any]]) -> tuple[int, dict[str, Any]]:
        _, fields = row
        decoded = {
            (key.decode() if isinstance(key, bytes) else str(key)): value
            for key, value in fields.items()
        }
        position_value = decoded["position"]
        event_value = decoded["event"]
        if isinstance(position_value, bytes):
            position_value = position_value.decode()
        return int(position_value), _json_loads(event_value)

    @staticmethod
    def _entry_timestamp_ms(entry_id: Any) -> int:
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode()
        return int(str(entry_id).split("-", 1)[0])

    @staticmethod
    def _next_stream_id(entry_id: Any) -> str:
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode()
        timestamp, sequence = str(entry_id).split("-", 1)
        return f"{timestamp}-{int(sequence) + 1}"


class RedisQueue(_RedisBackend):
    """Redis named queue with claim/ack semantics."""

    capabilities = BackendCapabilities(
        {"named_queues", "delayed", "visibility_timeout", "retry", "dead_letter", "inspect"}
    )

    async def submit(self, job: JobEnvelope, *, job_id: str | None = None) -> JobEnvelope:
        if job_id is not None:
            job = job.model_copy(update={"id": job_id})
        now = _now()
        visible_at = job.scheduled_for or now
        job.ready_since = visible_at if visible_at <= now else None
        async with self._queue_lock():
            existing = await self._get_job(job.id)
            if existing is None:
                # Written only if the id is still free; otherwise the envelope
                # stored meanwhile comes back and the same rule applies to it.
                stored = await self._client.eval(
                    _SUBMIT_SCRIPT,
                    4,
                    self._job_key(job.id),
                    self._queue_names_key(),
                    self._queue_jobs_key(job.queue),
                    self._ready_key(job.queue),
                    _job_to_json(job),
                    job.queue,
                    job.id,
                    repr(_score(visible_at)),
                )
                if stored is None:
                    return job
                existing = _job_from_json(stored)
            if existing.idempotency_payload() == job.idempotency_payload():
                return existing
            raise JobIdConflict(f"job id {job.id!r} already exists")

    async def claim(
        self, queues: list[str], *, visibility_timeout: float
    ) -> ClaimedJob | None:
        async with self._queue_lock():
            now = _now()
            lease_now = await self._server_now()
            await self._release_expired_claims_locked(now, lease_now)
            for queue in queues:
                ids = await self._client.zrangebyscore(
                    self._ready_key(queue),
                    "-inf",
                    _score(now),
                    start=0,
                    num=1,
                )
                if not ids:
                    continue
                job_id = self._decode(ids[0])
                queued = await self._get_job(job_id)
                if queued is None:
                    await self._forget_missing_job(queue, job_id)
                    continue
                token = uuid4().hex
                lease = max(visibility_timeout, queued.visibility_timeout or 0)
                expires_at = lease_now + timedelta(seconds=lease)
                # The script re-checks that the job is still ready and unclaimed
                # and hands back the envelope as stored at that moment.
                stored = await self._client.eval(
                    _CLAIM_SCRIPT,
                    4,
                    self._ready_key(queue),
                    self._claimed_key(queue),
                    self._claim_key(job_id),
                    self._job_key(job_id),
                    job_id,
                    repr(_score(now)),
                    repr(_score(expires_at)),
                    queue,
                    token,
                    expires_at.isoformat(),
                    "" if queued.visibility_timeout is None else repr(queued.visibility_timeout),
                )
                if stored is None:
                    continue
                stored_job, claim_order = stored
                job = _job_from_json(stored_job)
                job.ready_since = None
                return ClaimedJob(
                    job=job,
                    token=token,
                    visibility_timeout=lease,
                    claim_order=int(claim_order),
                )
            return None

    async def ack(self, queue: str, job_id: str, token: str) -> None:
        async with self._queue_lock():
            acked = await self._client.eval(
                _ACK_SCRIPT,
                7,
                self._claim_key(job_id),
                self._job_key(job_id),
                self._ready_key(queue),
                self._claimed_key(queue),
                self._dead_key(queue),
                self._dead_at_key(queue),
                self._queue_jobs_key(queue),
                queue,
                token,
                job_id,
            )
            if not acked:
                raise ValueError(f"Invalid claim token for job {job_id}")

    async def nack(
        self,
        queue: str,
        job_id: str,
        token: str,
        *,
        retry_at: datetime | None = None,
        dead_letter: bool = False,
        job: JobEnvelope | None = None,
    ) -> None:
        async with self._queue_lock():
            visible_at = retry_at or _now()
            if job is not None:
                job = job.model_copy(deep=True)
            else:
                job = await self._get_job(job_id)
            if job is None:
                raise ValueError(f"Invalid claim token for job {job_id}")
            job.ready_since = visible_at if visible_at <= _now() and not dead_letter else None
            nacked = await self._client.eval(
                _NACK_SCRIPT,
                6,
                self._claim_key(job_id),
                self._job_key(job_id),
                self._ready_key(queue),
                self._claimed_key(queue),
                self._dead_key(queue),
                self._dead_at_key(queue),
                queue,
                token,
                job_id,
                _job_to_json(job),
                "1" if dead_letter else "0",
                repr(_score(_now() if dead_letter else visible_at)),
                _job_to_json(job.model_copy(update={"ready_since": None})),
            )
            if not nacked:
                raise ValueError(f"Invalid claim token for job {job_id}")

    async def cancel(self, queue: str, job_id: str) -> bool:
        async with self._queue_lock():
            cancelled = await self._client.eval(
                _CANCEL_SCRIPT,
                7,
                self._claim_key(job_id),
                self._job_key(job_id),
                self._ready_key(queue),
                self._claimed_key(queue),
                self._dead_key(queue),
                self._dead_at_key(queue),
                self._queue_jobs_key(queue),
                job_id,
            )
            return bool(cancelled)

    async def wake(
        self, queue: str, job_id: str, *, resume_at: datetime | None = None
    ) -> bool:
        """Make the job ready at ``resume_at`` (default now).

        A claimed job keeps running; the wake is recorded on its claim and its
        worker's nack applies it. Returns ``False`` for a missing or
        dead-lettered job. The envelope is rewritten only if it is unchanged
        since it was read; after ``_WAKE_ATTEMPTS`` reads that each lost to a
        concurrent write, ``False`` is returned rather than retrying without
        bound.
        """
        async with self._queue_lock():
            for _ in range(_WAKE_ATTEMPTS):
                stored = await self._client.get(self._job_key(job_id))
                if stored is None:
                    return False
                visible_at = resume_at or _now()
                job = _job_from_json(stored)
                job.scheduled_for = visible_at
                job.ready_since = visible_at if visible_at <= _now() else None
                woken = await self._client.eval(
                    _WAKE_SCRIPT,
                    5,
                    self._job_key(job_id),
                    self._claim_key(job_id),
                    self._ready_key(queue),
                    self._claimed_key(queue),
                    self._dead_key(queue),
                    stored,
                    job_id,
                    _job_to_json(job),
                    repr(_score(visible_at)),
                )
                if woken in (1, 2):
                    return True
                if woken == -1:
                    return False
            return False

    async def stats(self, queue: str) -> QueueStats:
        async with self._queue_lock():
            now = _now()
            await self._release_expired_claims_locked(now, await self._server_now())
            ready_ids = await self._client.zrangebyscore(self._ready_key(queue), "-inf", _score(now))
            delayed_ids = await self._client.zrangebyscore(
                self._ready_key(queue),
                f"({_score(now)}",
                "+inf",
            )
            stats = QueueStats(
                queue=queue,
                ready=len(ready_ids),
                delayed=len(delayed_ids),
                claimed=int(await self._client.zcard(self._claimed_key(queue))),
                dead_lettered=int(await self._client.scard(self._dead_key(queue))),
            )
            for raw_id in ready_ids:
                job_id = self._decode(raw_id)
                job = await self._get_job(job_id)
                if job is None:
                    await self._forget_missing_job(queue, job_id)
                    continue
                # Read-only: a job with no ready_since has been ready since its
                # ready-set score.
                ready_since = job.ready_since
                if ready_since is None:
                    visible_at = await self._client.zscore(self._ready_key(queue), job_id)
                    ready_since = (
                        datetime.fromtimestamp(float(visible_at), tz=timezone.utc)
                        if visible_at is not None
                        else now
                    )
                stats.oldest_ready_age_seconds = max(
                    stats.oldest_ready_age_seconds,
                    (now - _utc(ready_since)).total_seconds(),
                )
            return stats

    async def prune_dead_markers(self, *, max_age_seconds: float) -> int:
        async with self._queue_lock():
            cutoff = _score(_now() - timedelta(seconds=max_age_seconds))
            count = 0
            queue_names = [
                self._decode(raw)
                for raw in await self._client.smembers(self._queue_names_key())
            ]
            for queue in queue_names:
                expired = await self._client.zrangebyscore(
                    self._dead_at_key(queue),
                    "-inf",
                    cutoff,
                )
                for raw_id in expired:
                    count += await self._prune_dead_job(queue, self._decode(raw_id), cutoff)
            return count

    def _queue_lock(self):
        return self._client.lock(self._key("queue", "lock"), timeout=10)

    async def _get_job(self, job_id: str) -> JobEnvelope | None:
        value = await self._client.get(self._job_key(job_id))
        return _job_from_json(value) if value is not None else None

    async def _server_now(self) -> datetime:
        """The Redis server's clock, which claim leases are set and judged by.

        Worker processes with skewed clocks then agree on when a lease expires.
        """
        seconds, microseconds = await self._client.time()
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc) + timedelta(
            microseconds=int(microseconds)
        )

    async def _release_expired_claims(self, now: datetime) -> None:
        """Release claims whose lease has lapsed, making the jobs ready at ``now``.

        Expiry itself is judged by the server clock. Takes the queue lock like
        claim, ack and nack; each release is also conditional (see
        ``_RELEASE_SCRIPT``), so a claim made after this reaper read the job is
        never erased, even if the lock has expired by then.
        """
        async with self._queue_lock():
            await self._release_expired_claims_locked(now, await self._server_now())

    async def _release_expired_claims_locked(self, now: datetime, lease_now: datetime) -> None:
        queue_names = [self._decode(raw) for raw in await self._client.smembers(self._queue_names_key())]
        for queue in queue_names:
            expired = await self._client.zrangebyscore(
                self._claimed_key(queue),
                "-inf",
                _score(lease_now),
            )
            for raw_id in expired:
                job_id = self._decode(raw_id)
                token = await self._client.hget(self._claim_key(job_id), "token")
                job = await self._get_job(job_id)
                if job is None:
                    await self._forget_missing_job(queue, job_id)
                    continue
                job.reclaim_count += 1
                job.ready_since = now
                await self._client.eval(
                    _RELEASE_SCRIPT,
                    4,
                    self._claim_key(job_id),
                    self._job_key(job_id),
                    self._ready_key(queue),
                    self._claimed_key(queue),
                    self._decode(token) if token is not None else "",
                    job_id,
                    _job_to_json(job),
                    repr(_score(lease_now)),
                    repr(_score(now)),
                )

    async def _forget_missing_job(self, queue: str, job_id: str) -> None:
        await self._client.eval(
            _FORGET_MISSING_JOB_SCRIPT,
            6,
            self._job_key(job_id),
            self._ready_key(queue),
            self._claimed_key(queue),
            self._dead_key(queue),
            self._dead_at_key(queue),
            self._queue_jobs_key(queue),
            job_id,
        )

    async def _prune_dead_job(self, queue: str, job_id: str, cutoff: float) -> int:
        return int(
            await self._client.eval(
                _PRUNE_DEAD_JOB_SCRIPT,
                7,
                self._dead_at_key(queue),
                self._dead_key(queue),
                self._claim_key(job_id),
                self._job_key(job_id),
                self._ready_key(queue),
                self._claimed_key(queue),
                self._queue_jobs_key(queue),
                job_id,
                repr(cutoff),
            )
        )

    def _queue_names_key(self) -> str:
        return self._key("queue", "names")

    def _job_key(self, job_id: str) -> str:
        return self._key("queue", "jobs", job_id)

    def _claim_key(self, job_id: str) -> str:
        return self._key("queue", "claims", job_id)

    def _ready_key(self, queue: str) -> str:
        return self._key("queue", "ready", queue)

    def _claimed_key(self, queue: str) -> str:
        return self._key("queue", "claimed", queue)

    def _dead_key(self, queue: str) -> str:
        return self._key("queue", "dead", queue)

    def _dead_at_key(self, queue: str) -> str:
        return self._key("queue", "dead_at", queue)

    def _queue_jobs_key(self, queue: str) -> str:
        return self._key("queue", "jobs_by_queue", queue)

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)
