"""In-memory worker backends for local mode and tests."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import islice
from typing import Any
from uuid import uuid4

from skrift.workers.interfaces import TTL, BackendCapabilities, UpdateFn, resolve_ttl
from skrift.workers.models import (
    ClaimedJob,
    DeadJobEntry,
    EventIdConflict,
    JobEnvelope,
    JobIdConflict,
    JobState,
    JobStatus,
    QueueStats,
)

# Streams live entirely in process memory, so they are capped the way the Redis
# event log is capped by `retention.redis_event_max_entries`: newest events win
# and the oldest are dropped once the cap is reached.
DEFAULT_EVENT_STREAM_MAX_EVENTS = 10_000

WORKER_JOB_STATE_PREFIX = "workers:jobs:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _StoredValue:
    value: Any
    expires_at: datetime | None = None


class InMemoryStateStore:
    """Process-local state store backed by a dict and one lock."""

    capabilities = BackendCapabilities({"ttl", "atomic_update", "prefix_scan"})

    def __init__(self) -> None:
        self._values: dict[str, _StoredValue] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, stored: _StoredValue) -> bool:
        return stored.expires_at is not None and stored.expires_at <= _now()

    async def get(self, key: str) -> Any:
        async with self._lock:
            stored = self._values.get(key)
            if stored is None:
                return None
            if self._is_expired(stored):
                del self._values[key]
                return None
            return stored.value

    async def set(self, key: str, value: Any, *, ttl: TTL = None) -> None:
        resolved_ttl = resolve_ttl(ttl, value)
        expires_at = _now() + timedelta(seconds=resolved_ttl) if resolved_ttl is not None else None
        async with self._lock:
            self._values[key] = _StoredValue(value=value, expires_at=expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)

    async def update(self, key: str, fn: UpdateFn, *, ttl: TTL = None) -> Any:
        async with self._lock:
            current = self._values.get(key)
            current_value = None
            if current is not None and not self._is_expired(current):
                current_value = current.value
            next_value = fn(current_value)
            if inspect.isawaitable(next_value):
                next_value = await next_value
            resolved_ttl = resolve_ttl(ttl, next_value)
            expires_at = (
                _now() + timedelta(seconds=resolved_ttl) if resolved_ttl is not None else None
            )
            self._values[key] = _StoredValue(value=next_value, expires_at=expires_at)
            return next_value

    async def keys(self, prefix: str = "") -> list[str]:
        async with self._lock:
            for key in list(self._values):
                if self._is_expired(self._values[key]):
                    del self._values[key]
            return sorted(key for key in self._values if key.startswith(prefix))

    async def sweep_expired(self) -> int:
        """Drop every expired entry and report how many were removed.

        Reads skip expired entries without necessarily reclaiming them, so this
        dedicated sweep — driven by the runtime's reaper timer — is what actually
        frees the memory they hold.
        """
        async with self._lock:
            expired = [key for key, stored in self._values.items() if self._is_expired(stored)]
            for key in expired:
                del self._values[key]
            return len(expired)

    async def worker_job_states(self, *, limit: int | None = None) -> tuple[list[JobState], int]:
        """Return recent worker job states plus the total, in one pass."""
        async with self._lock:
            states = [
                stored.value
                for key, stored in self._values.items()
                if key.startswith(WORKER_JOB_STATE_PREFIX)
                and not self._is_expired(stored)
                and isinstance(stored.value, JobState)
            ]
        states.sort(key=lambda state: state.updated_at, reverse=True)
        return (states if limit is None else states[:limit]), len(states)

    async def worker_job_counts(self) -> dict[str, int]:
        """Return aggregate worker job counts without re-reading every entry."""
        active_statuses = {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.PAUSED}
        states, total = await self.worker_job_states()
        return {
            "total": total,
            "active": sum(state.status in active_statuses for state in states),
        }


class _EventStream:
    """Bounded event buffer whose positions stay stable as old events drop out."""

    def __init__(self, max_events: int) -> None:
        self._events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=max_events)
        self._positions_by_event_id: dict[Any, int] = {}
        self._next_position = 0

    @property
    def next_position(self) -> int:
        return self._next_position

    @property
    def event_id_count(self) -> int:
        return len(self._positions_by_event_id)

    def find_by_event_id(self, event_id: Any) -> tuple[int, dict[str, Any]] | None:
        position = self._positions_by_event_id.get(event_id)
        return None if position is None else (position, self._at(position))

    def append(self, event: dict[str, Any]) -> int:
        position = self._next_position
        evicted = self._events[0][1] if self._is_full() else None
        self._events.append((position, dict(event)))
        if evicted is not None:
            self._forget_event_id(evicted)
        event_id = event.get("event_id")
        if event_id is not None:
            self._positions_by_event_id[event_id] = position
        self._next_position = position + 1
        return position

    def read(self, *, from_position: int, limit: int | None) -> list[tuple[int, dict[str, Any]]]:
        start = max(0, from_position - self._start_position)
        end = None if limit is None else start + limit
        return [(position, dict(event)) for position, event in islice(self._events, start, end)]

    def read_tail(self, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
        start = max(0, len(self._events) - limit)
        return [(position, dict(event)) for position, event in islice(self._events, start, None)]

    def _is_full(self) -> bool:
        return bool(self._events) and len(self._events) == self._events.maxlen

    @property
    def _start_position(self) -> int:
        return self._events[0][0] if self._events else self._next_position

    def _at(self, position: int) -> dict[str, Any]:
        return self._events[position - self._start_position][1]

    def _forget_event_id(self, event: dict[str, Any]) -> None:
        event_id = event.get("event_id")
        if event_id is not None:
            self._positions_by_event_id.pop(event_id, None)


class InMemoryEventLog:
    """Append-only event log with replay and live tail support.

    Each stream keeps only its most recent ``max_events_per_stream`` events;
    positions remain monotonic so cursors held by subscribers and the persister
    stay meaningful after older events are dropped.
    """

    capabilities = BackendCapabilities({"replay", "live_tail", "delete"})

    def __init__(
        self, *, max_events_per_stream: int = DEFAULT_EVENT_STREAM_MAX_EVENTS
    ) -> None:
        self.max_events_per_stream = max_events_per_stream
        self._streams: dict[str, _EventStream] = defaultdict(self._new_stream)
        self._condition = asyncio.Condition()

    def _new_stream(self) -> _EventStream:
        return _EventStream(self.max_events_per_stream)

    def stream_event_id_count(self, stream: str) -> int:
        """Number of ``event_id`` dedupe entries a stream currently retains.

        The dedupe index is bounded by the stream itself, so this stays at or
        below ``max_events_per_stream``.
        """
        events = self._streams.get(stream)
        return 0 if events is None else events.event_id_count

    async def append(self, stream: str, event: dict[str, Any]) -> int:
        async with self._condition:
            event_id = event.get("event_id")
            if event_id is not None:
                existing = self._streams[stream].find_by_event_id(event_id)
                if existing is not None:
                    position, stored = existing
                    if stored == event:
                        return position
                    raise EventIdConflict(
                        f"event_id {event_id!r} already exists in stream {stream!r}"
                    )
            position = self._streams[stream].append(event)
            self._condition.notify_all()
            return position

    async def read(
        self, stream: str, *, from_position: int = 0, limit: int | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        async with self._condition:
            events = self._streams.get(stream)
            if events is None:
                return []
            return events.read(from_position=from_position, limit=limit)

    async def read_tail(self, stream: str, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
        if limit <= 0:
            return []
        async with self._condition:
            events = self._streams.get(stream)
            return [] if events is None else events.read_tail(limit=limit)

    async def read_filtered(
        self,
        stream: str,
        *,
        filters: dict[str, Any],
        from_position: int = 0,
        limit: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        rows = await self.read(stream, from_position=from_position)
        matches = [
            (position, event)
            for position, event in rows
            if all(event.get(key) == value for key, value in filters.items())
        ]
        return matches if limit is None else matches[:limit]

    async def subscribe(
        self, stream: str, *, from_position: int | None = None
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        cursor = self._next_position(stream) if from_position is None else from_position
        while True:
            async with self._condition:
                while cursor >= self._next_position(stream):
                    await self._condition.wait()
                # Events dropped by the size cap are skipped rather than replayed.
                position, event = self._streams[stream].read(from_position=cursor, limit=1)[0]
                cursor = position + 1
            yield position, event

    def _next_position(self, stream: str) -> int:
        events = self._streams.get(stream)
        return 0 if events is None else events.next_position

    async def delete(self, stream: str) -> None:
        async with self._condition:
            self._streams.pop(stream, None)
            self._condition.notify_all()

    async def list_streams(self, prefix: str = "") -> list[str]:
        async with self._condition:
            return sorted(stream for stream in self._streams if stream.startswith(prefix))


@dataclass
class _QueueEntry:
    job: JobEnvelope
    visible_at: datetime
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    dead_lettered: bool = False


class InMemoryQueue:
    """Process-local named queue with claim/ack/nack semantics."""

    capabilities = BackendCapabilities(
        {"named_queues", "delayed", "visibility_timeout", "retry", "dead_letter", "inspect"}
    )

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, _QueueEntry]] = defaultdict(dict)
        self._condition = asyncio.Condition()

    async def submit(self, job: JobEnvelope, *, job_id: str | None = None) -> JobEnvelope:
        if job_id is not None:
            job = job.model_copy(update={"id": job_id})
        visible_at = job.scheduled_for or _now()
        job.ready_since = visible_at if visible_at <= _now() else None
        async with self._condition:
            existing = self._entries[job.queue].get(job.id)
            if existing is not None:
                if existing.job.idempotency_payload() == job.idempotency_payload():
                    return existing.job
                raise JobIdConflict(f"job id {job.id!r} already exists")
            self._entries[job.queue][job.id] = _QueueEntry(job=job, visible_at=visible_at)
            self._condition.notify_all()
            return job

    async def _release_expired_claims(self, now: datetime) -> None:
        """Reclaim jobs whose visibility timeout lapsed, as of ``now``.

        Mirrors the Redis and SQLAlchemy queues so the runtime reaper can drive
        every backend through the same call.
        """
        async with self._condition:
            self._release_expired_claims_locked(now)
            self._condition.notify_all()

    def _release_expired_claims_locked(self, now: datetime) -> None:
        for queue_entries in self._entries.values():
            for entry in queue_entries.values():
                if (
                    entry.claim_token is not None
                    and entry.claim_expires_at is not None
                    and entry.claim_expires_at <= now
                    and not entry.dead_lettered
                ):
                    entry.claim_token = None
                    entry.claim_expires_at = None
                    entry.visible_at = now
                    entry.job.ready_since = now
                    entry.job.reclaim_count += 1

    def _claimable(self, queue: str) -> _QueueEntry | None:
        now = _now()
        for entry in self._entries.get(queue, {}).values():
            if entry.dead_lettered or entry.claim_token is not None:
                continue
            if entry.visible_at <= now:
                if entry.job.ready_since is None:
                    entry.job.ready_since = entry.visible_at
                return entry
        return None

    async def claim(
        self, queues: list[str], *, visibility_timeout: float
    ) -> ClaimedJob | None:
        async with self._condition:
            self._release_expired_claims_locked(_now())
            for queue in queues:
                entry = self._claimable(queue)
                if entry is None:
                    continue
                token = uuid4().hex
                entry.claim_token = token
                entry.claim_expires_at = _now() + timedelta(seconds=visibility_timeout)
                entry.job.ready_since = None
                return ClaimedJob(job=entry.job, token=token)
            return None

    async def ack(self, queue: str, job_id: str, token: str) -> None:
        async with self._condition:
            entry = self._entries.get(queue, {}).get(job_id)
            if entry is None or entry.claim_token != token:
                raise ValueError(f"Invalid claim token for job {job_id}")
            del self._entries[queue][job_id]
            self._condition.notify_all()

    async def nack(
        self,
        queue: str,
        job_id: str,
        token: str,
        *,
        retry_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> None:
        async with self._condition:
            entry = self._entries.get(queue, {}).get(job_id)
            if entry is None or entry.claim_token != token:
                raise ValueError(f"Invalid claim token for job {job_id}")
            entry.claim_token = None
            entry.claim_expires_at = None
            entry.dead_lettered = dead_letter
            entry.visible_at = retry_at or _now()
            entry.job.ready_since = (
                entry.visible_at if entry.visible_at <= _now() and not dead_letter else None
            )
            self._condition.notify_all()

    async def cancel(self, queue: str, job_id: str) -> bool:
        async with self._condition:
            entry = self._entries.get(queue, {}).get(job_id)
            if entry is None or entry.claim_token is not None:
                return False
            del self._entries[queue][job_id]
            self._condition.notify_all()
            return True

    async def wake(
        self, queue: str, job_id: str, *, resume_at: datetime | None = None
    ) -> bool:
        async with self._condition:
            entry = self._entries.get(queue, {}).get(job_id)
            if entry is None or entry.dead_lettered:
                return False
            entry.visible_at = resume_at or _now()
            entry.job.scheduled_for = entry.visible_at
            entry.job.ready_since = entry.visible_at if entry.visible_at <= _now() else None
            self._condition.notify_all()
            return True

    async def stats(self, queue: str) -> QueueStats:
        async with self._condition:
            now = _now()
            self._release_expired_claims_locked(now)
            stats = QueueStats(queue=queue)
            for entry in self._entries.get(queue, {}).values():
                if entry.dead_lettered:
                    stats.dead_lettered += 1
                elif entry.claim_token is not None:
                    stats.claimed += 1
                elif entry.visible_at > now:
                    stats.delayed += 1
                    entry.job.ready_since = None
                else:
                    stats.ready += 1
                    if entry.job.ready_since is None:
                        entry.job.ready_since = entry.visible_at
                    stats.oldest_ready_age_seconds = max(
                        stats.oldest_ready_age_seconds,
                        (now - entry.job.ready_since).total_seconds(),
                    )
            return stats


class InMemoryArchive:
    """Minimal in-memory archive used by the MVP smoke tests."""

    capabilities = BackendCapabilities({"events", "snapshots", "history"})

    def __init__(self) -> None:
        self._events: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        self._snapshots: dict[str, list[tuple[datetime, Any]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def bulk_insert_events(
        self, events: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        async with self._lock:
            for stream, position, event in events:
                self._events[stream].append((position, dict(event)))

    async def upsert_state_snapshot(
        self, key: str, value: Any, *, timestamp: datetime | None = None
    ) -> None:
        async with self._lock:
            self._snapshots[key].append((timestamp or _now(), value))

    async def query_events(
        self,
        stream: str,
        *,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        async with self._lock:
            return [
                (position, dict(event))
                for position, event in self._events.get(stream, [])
                if position >= from_position and (to_position is None or position <= to_position)
            ]

    async def latest_state_snapshot(self, key: str) -> Any:
        async with self._lock:
            snapshots = self._snapshots.get(key, [])
            return snapshots[-1][1] if snapshots else None

    async def historical_state_snapshots(self, key: str) -> list[tuple[datetime, Any]]:
        async with self._lock:
            return list(self._snapshots.get(key, []))


class InMemoryDeadLetterStore:
    """Process-local DLQ records for local mode and tests."""

    capabilities = BackendCapabilities({"inspect", "replay", "discard", "export"})

    def __init__(self) -> None:
        self._entries: dict[str, DeadJobEntry] = {}
        self._lock = asyncio.Lock()

    async def create(self, entry: DeadJobEntry) -> DeadJobEntry:
        async with self._lock:
            stored = entry.model_copy(deep=True)
            self._entries[stored.id] = stored
            return stored.model_copy(deep=True)

    async def get(self, entry_id: str) -> DeadJobEntry | None:
        async with self._lock:
            entry = self._entries.get(entry_id)
            return entry.model_copy(deep=True) if entry is not None else None

    async def list(
        self,
        *,
        queue: str | None = None,
        job_type: str | None = None,
        cause: str | None = None,
        state: str | None = None,
        exception_type: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list[DeadJobEntry]:
        async with self._lock:
            entries = list(self._entries.values())
        if queue:
            entries = [entry for entry in entries if entry.queue == queue]
        if job_type:
            entries = [entry for entry in entries if entry.job_type == job_type]
        if cause:
            entries = [entry for entry in entries if entry.cause == cause]
        if state:
            entries = [entry for entry in entries if entry.state == state]
        if exception_type:
            entries = [
                entry
                for entry in entries
                if any(attempt.exception_type == exception_type for attempt in entry.attempts)
            ]
        if created_after:
            entries = [entry for entry in entries if entry.created_at >= created_after]
        if created_before:
            entries = [entry for entry in entries if entry.created_at <= created_before]
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return [entry.model_copy(deep=True) for entry in entries]

    async def save(self, entry: DeadJobEntry) -> DeadJobEntry:
        async with self._lock:
            stored = entry.model_copy(deep=True)
            stored.updated_at = _now()
            self._entries[stored.id] = stored
            return stored.model_copy(deep=True)
