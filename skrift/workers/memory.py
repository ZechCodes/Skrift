"""In-memory worker backends for local mode and tests."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from skrift.workers.interfaces import BackendCapabilities, UpdateFn
from skrift.workers.models import ClaimedJob, DeadJobEntry, JobEnvelope, QueueStats


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

    async def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        expires_at = _now() + timedelta(seconds=ttl) if ttl is not None else None
        async with self._lock:
            self._values[key] = _StoredValue(value=value, expires_at=expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)

    async def update(self, key: str, fn: UpdateFn, *, ttl: float | None = None) -> Any:
        expires_at = _now() + timedelta(seconds=ttl) if ttl is not None else None
        async with self._lock:
            current = self._values.get(key)
            current_value = None
            if current is not None and not self._is_expired(current):
                current_value = current.value
            next_value = fn(current_value)
            if inspect.isawaitable(next_value):
                next_value = await next_value
            self._values[key] = _StoredValue(value=next_value, expires_at=expires_at)
            return next_value

    async def keys(self, prefix: str = "") -> list[str]:
        async with self._lock:
            for key in list(self._values):
                if self._is_expired(self._values[key]):
                    del self._values[key]
            return sorted(key for key in self._values if key.startswith(prefix))


class InMemoryEventLog:
    """Append-only event log with replay and live tail support."""

    capabilities = BackendCapabilities({"replay", "live_tail", "delete"})

    def __init__(self) -> None:
        self._streams: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._condition = asyncio.Condition()

    async def append(self, stream: str, event: dict[str, Any]) -> int:
        async with self._condition:
            self._streams[stream].append(dict(event))
            position = len(self._streams[stream]) - 1
            self._condition.notify_all()
            return position

    async def read(
        self, stream: str, *, from_position: int = 0, limit: int | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        async with self._condition:
            events = self._streams.get(stream, [])
            end = None if limit is None else from_position + limit
            return [
                (position, dict(event))
                for position, event in enumerate(events[from_position:end], start=from_position)
            ]

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
        cursor = len(self._streams.get(stream, [])) if from_position is None else from_position
        while True:
            async with self._condition:
                while cursor >= len(self._streams.get(stream, [])):
                    await self._condition.wait()
                event = dict(self._streams[stream][cursor])
                position = cursor
                cursor += 1
            yield position, event

    async def delete(self, stream: str) -> None:
        async with self._condition:
            self._streams.pop(stream, None)
            self._condition.notify_all()


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

    async def submit(self, job: JobEnvelope) -> None:
        visible_at = job.scheduled_for or _now()
        job.ready_since = visible_at if visible_at <= _now() else None
        async with self._condition:
            self._entries[job.queue][job.id] = _QueueEntry(job=job, visible_at=visible_at)
            self._condition.notify_all()

    def _release_expired_claims(self) -> None:
        now = _now()
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
            self._release_expired_claims()
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
            self._release_expired_claims()
            stats = QueueStats(queue=queue)
            now = _now()
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
