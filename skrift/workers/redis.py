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

from skrift.workers.interfaces import BackendCapabilities, UpdateFn
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

    async def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        redis_key = self._state_key(key)
        payload = _json_dumps(_value_to_json(value))
        if ttl is None:
            await self._client.set(redis_key, payload)
        else:
            await self._client.set(redis_key, payload, px=max(1, int(ttl * 1000)))
        await self._client.sadd(self._key("state", "keys"), key)
        if key.startswith("workers:jobs:"):
            await self._index_worker_job_state(key, value)

    async def delete(self, key: str) -> None:
        await self._client.delete(self._state_key(key))
        await self._client.srem(self._key("state", "keys"), key)
        if key.startswith("workers:jobs:"):
            await self._client.zrem(self._key("state", "worker_jobs"), key)
            await self._client.srem(self._key("state", "worker_jobs_active"), key)

    async def update(self, key: str, fn: UpdateFn, *, ttl: float | None = None) -> Any:
        async with self._client.lock(self._key("state", "locks", key), timeout=10):
            current = await self.get(key)
            next_value = fn(current)
            if inspect.isawaitable(next_value):
                next_value = await next_value
            await self.set(key, next_value, ttl=ttl)
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
        if stale:
            await self._client.srem(self._key("state", "keys"), *stale)
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
        if stale:
            await self._client.zrem(job_index, *stale)
            await self._client.srem(self._key("state", "worker_jobs_active"), *stale)
            total = max(0, total - len(stale))
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
                await self._client.zrem(job_index, key)
                await self._client.srem(self._key("state", "worker_jobs_active"), key)
                continue
            if isinstance(state, JobState) and state.status in terminal:
                await self.delete(key)
                count += 1
        return count

    def _state_key(self, key: str) -> str:
        return self._key("state", "values", key)

    async def _index_worker_job_state(self, key: str, value: Any) -> None:
        if not isinstance(value, JobState):
            value = _value_from_json(value)
        if not isinstance(value, JobState):
            return
        await self._client.zadd(
            self._key("state", "worker_jobs"),
            {key: _score(value.updated_at)},
        )
        active_key = self._key("state", "worker_jobs_active")
        if value.status in {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.PAUSED}:
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
            if existing is not None:
                if existing.idempotency_payload() == job.idempotency_payload():
                    return existing
                raise JobIdConflict(f"job id {job.id!r} already exists")
            await self._client.set(self._job_key(job.id), _job_to_json(job))
            await self._client.sadd(self._queue_names_key(), job.queue)
            await self._client.sadd(self._queue_jobs_key(job.queue), job.id)
            await self._client.zadd(self._ready_key(job.queue), {job.id: _score(visible_at)})
            return job

    async def claim(
        self, queues: list[str], *, visibility_timeout: float
    ) -> ClaimedJob | None:
        async with self._queue_lock():
            now = _now()
            await self._release_expired_claims(now)
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
                job = await self._get_job(job_id)
                if job is None:
                    await self._remove_job_indexes(queue, job_id)
                    continue
                if job.ready_since is None:
                    visible_at = await self._client.zscore(self._ready_key(queue), job_id)
                    job.ready_since = (
                        datetime.fromtimestamp(float(visible_at), tz=timezone.utc)
                        if visible_at is not None
                        else now
                    )
                job.ready_since = None
                token = uuid4().hex
                expires_at = now + timedelta(seconds=visibility_timeout)
                await self._client.set(self._job_key(job_id), _job_to_json(job))
                await self._client.zrem(self._ready_key(queue), job_id)
                await self._client.zadd(self._claimed_key(queue), {job_id: _score(expires_at)})
                await self._client.hset(
                    self._claim_key(job_id),
                    mapping={
                        "queue": queue,
                        "token": token,
                        "expires_at": expires_at.isoformat(),
                    },
                )
                return ClaimedJob(job=job, token=token)
            return None

    async def ack(self, queue: str, job_id: str, token: str) -> None:
        async with self._queue_lock():
            await self._assert_claim(queue, job_id, token)
            await self._delete_job(queue, job_id)

    async def nack(
        self,
        queue: str,
        job_id: str,
        token: str,
        *,
        retry_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> None:
        async with self._queue_lock():
            await self._assert_claim(queue, job_id, token)
            visible_at = retry_at or _now()
            job = await self._get_job(job_id)
            if job is None:
                raise ValueError(f"Invalid claim token for job {job_id}")
            job.ready_since = visible_at if visible_at <= _now() and not dead_letter else None
            await self._client.set(self._job_key(job_id), _job_to_json(job))
            await self._client.delete(self._claim_key(job_id))
            await self._client.zrem(self._claimed_key(queue), job_id)
            if dead_letter:
                await self._client.zrem(self._ready_key(queue), job_id)
                await self._client.sadd(self._dead_key(queue), job_id)
                await self._client.zadd(self._dead_at_key(queue), {job_id: _score(_now())})
            else:
                await self._client.zadd(self._ready_key(queue), {job_id: _score(visible_at)})

    async def cancel(self, queue: str, job_id: str) -> bool:
        async with self._queue_lock():
            if await self._client.exists(self._claim_key(job_id)):
                return False
            if not await self._client.exists(self._job_key(job_id)):
                return False
            await self._delete_job(queue, job_id)
            return True

    async def wake(
        self, queue: str, job_id: str, *, resume_at: datetime | None = None
    ) -> bool:
        async with self._queue_lock():
            if await self._client.sismember(self._dead_key(queue), job_id):
                return False
            job = await self._get_job(job_id)
            if job is None:
                return False
            visible_at = resume_at or _now()
            job.scheduled_for = visible_at
            job.ready_since = visible_at if visible_at <= _now() else None
            await self._client.set(self._job_key(job_id), _job_to_json(job))
            await self._client.zrem(self._claimed_key(queue), job_id)
            await self._client.delete(self._claim_key(job_id))
            await self._client.zadd(self._ready_key(queue), {job_id: _score(visible_at)})
            return True

    async def stats(self, queue: str) -> QueueStats:
        async with self._queue_lock():
            now = _now()
            await self._release_expired_claims(now)
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
                    await self._remove_job_indexes(queue, job_id)
                    continue
                if job.ready_since is None:
                    visible_at = await self._client.zscore(self._ready_key(queue), job_id)
                    job.ready_since = (
                        datetime.fromtimestamp(float(visible_at), tz=timezone.utc)
                        if visible_at is not None
                        else now
                    )
                    await self._client.set(self._job_key(job_id), _job_to_json(job))
                stats.oldest_ready_age_seconds = max(
                    stats.oldest_ready_age_seconds,
                    (now - _utc(job.ready_since)).total_seconds(),
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
                    job_id = self._decode(raw_id)
                    await self._delete_job(queue, job_id)
                    count += 1
            return count

    def _queue_lock(self):
        return self._client.lock(self._key("queue", "lock"), timeout=10)

    async def _get_job(self, job_id: str) -> JobEnvelope | None:
        value = await self._client.get(self._job_key(job_id))
        return _job_from_json(value) if value is not None else None

    async def _assert_claim(self, queue: str, job_id: str, token: str) -> None:
        claim = await self._client.hgetall(self._claim_key(job_id))
        decoded = {self._decode(key): self._decode(value) for key, value in claim.items()}
        if decoded.get("queue") != queue or decoded.get("token") != token:
            raise ValueError(f"Invalid claim token for job {job_id}")

    async def _release_expired_claims(self, now: datetime) -> None:
        queue_names = [self._decode(raw) for raw in await self._client.smembers(self._queue_names_key())]
        for queue in queue_names:
            expired = await self._client.zrangebyscore(
                self._claimed_key(queue),
                "-inf",
                _score(now),
            )
            for raw_id in expired:
                job_id = self._decode(raw_id)
                job = await self._get_job(job_id)
                if job is None:
                    await self._remove_job_indexes(queue, job_id)
                    continue
                job.reclaim_count += 1
                job.ready_since = now
                await self._client.set(self._job_key(job_id), _job_to_json(job))
                await self._client.delete(self._claim_key(job_id))
                await self._client.zrem(self._claimed_key(queue), job_id)
                await self._client.zadd(self._ready_key(queue), {job_id: _score(now)})

    async def _delete_job(self, queue: str, job_id: str) -> None:
        await self._client.delete(self._job_key(job_id), self._claim_key(job_id))
        await self._remove_job_indexes(queue, job_id)

    async def _remove_job_indexes(self, queue: str, job_id: str) -> None:
        await self._client.zrem(self._ready_key(queue), job_id)
        await self._client.zrem(self._claimed_key(queue), job_id)
        await self._client.srem(self._dead_key(queue), job_id)
        await self._client.zrem(self._dead_at_key(queue), job_id)
        await self._client.srem(self._queue_jobs_key(queue), job_id)

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
