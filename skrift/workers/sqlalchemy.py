"""SQLAlchemy-backed worker persistence implementations."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from skrift.db.models.worker import (
    WorkerArchiveEventRecord,
    WorkerArchiveSnapshotRecord,
    WorkerDeadLetterRecord,
    WorkerEventRecord,
    WorkerQueueRecord,
    WorkerStateRecord,
)
from skrift.workers.interfaces import BackendCapabilities, UpdateFn
from skrift.workers.models import (
    ClaimedJob,
    DeadJobEntry,
    DeadLetterState,
    JobEnvelope,
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


def _job_to_json(job: JobEnvelope) -> dict[str, Any]:
    return job.model_dump(mode="json")


def _entry_to_json(entry: DeadJobEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json")


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


class _SQLAlchemyBackend:
    def __init__(self, *, session_maker: Any, **_: Any) -> None:
        self._session_maker = session_maker


class SQLAlchemyStateStore(_SQLAlchemyBackend):
    """SQLAlchemy key/value state store with TTL support."""

    capabilities = BackendCapabilities({"ttl", "atomic_update", "prefix_scan"})

    async def get(self, key: str) -> Any:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerStateRecord).where(WorkerStateRecord.key == key)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            if record.expires_at is not None and _utc(record.expires_at) <= _now():
                await session.delete(record)
                await session.commit()
                return None
            return _value_from_json(record.value)

    async def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        expires_at = _now() + timedelta(seconds=ttl) if ttl is not None else None
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerStateRecord).where(WorkerStateRecord.key == key)
            )
            record = result.scalar_one_or_none()
            if record is None:
                session.add(
                    WorkerStateRecord(
                        key=key,
                        value=_value_to_json(value),
                        expires_at=expires_at,
                    )
                )
            else:
                record.value = _value_to_json(value)
                record.expires_at = expires_at
            await session.commit()

    async def delete(self, key: str) -> None:
        async with self._session_maker() as session:
            await session.execute(delete(WorkerStateRecord).where(WorkerStateRecord.key == key))
            await session.commit()

    async def update(self, key: str, fn: UpdateFn, *, ttl: float | None = None) -> Any:
        expires_at = _now() + timedelta(seconds=ttl) if ttl is not None else None
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerStateRecord)
                .where(WorkerStateRecord.key == key)
                .with_for_update()
            )
            record = result.scalar_one_or_none()
            current_value = None
            if record is not None and (
                record.expires_at is None or _utc(record.expires_at) > _now()
            ):
                current_value = _value_from_json(record.value)
            next_value = fn(current_value)
            if inspect.isawaitable(next_value):
                next_value = await next_value
            if record is None:
                session.add(
                    WorkerStateRecord(
                        key=key,
                        value=_value_to_json(next_value),
                        expires_at=expires_at,
                    )
                )
            else:
                record.value = _value_to_json(next_value)
                record.expires_at = expires_at
            await session.commit()
            return next_value

    async def keys(self, prefix: str = "") -> list[str]:
        async with self._session_maker() as session:
            await session.execute(
                delete(WorkerStateRecord).where(
                    WorkerStateRecord.expires_at.is_not(None),
                    WorkerStateRecord.expires_at <= _now(),
                )
            )
            await session.commit()
            result = await session.execute(
                select(WorkerStateRecord.key)
                .where(WorkerStateRecord.key.like(f"{prefix}%"))
                .order_by(WorkerStateRecord.key)
            )
            return list(result.scalars().all())

    async def worker_job_states(self, *, limit: int | None = None) -> tuple[list[JobState], int]:
        """Return recent worker job states without scanning every persisted key."""
        prefix = "workers:jobs:"
        async with self._session_maker() as session:
            await session.execute(
                delete(WorkerStateRecord).where(
                    WorkerStateRecord.expires_at.is_not(None),
                    WorkerStateRecord.expires_at <= _now(),
                )
            )
            await session.commit()
            total = await session.scalar(
                select(func.count()).select_from(WorkerStateRecord).where(
                    WorkerStateRecord.key.like(f"{prefix}%")
                )
            )
            stmt = (
                select(WorkerStateRecord.value)
                .where(WorkerStateRecord.key.like(f"{prefix}%"))
                .order_by(WorkerStateRecord.updated_at.desc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return [
                state
                for value in result.scalars().all()
                if isinstance((state := _value_from_json(value)), JobState)
            ], int(total or 0)

    async def worker_job_counts(self) -> dict[str, int]:
        """Return aggregate worker job counts without loading every state row."""
        prefix = "workers:jobs:"
        active_statuses = [
            JobStatus.CLAIMED.value,
            JobStatus.RUNNING.value,
            JobStatus.PAUSED.value,
        ]
        async with self._session_maker() as session:
            await session.execute(
                delete(WorkerStateRecord).where(
                    WorkerStateRecord.expires_at.is_not(None),
                    WorkerStateRecord.expires_at <= _now(),
                )
            )
            await session.commit()
            total = await session.scalar(
                select(func.count()).select_from(WorkerStateRecord).where(
                    WorkerStateRecord.key.like(f"{prefix}%")
                )
            )
            active = await session.scalar(
                select(func.count()).select_from(WorkerStateRecord).where(
                    WorkerStateRecord.key.like(f"{prefix}%"),
                    WorkerStateRecord.value["value"]["status"].as_string().in_(active_statuses),
                )
            )
            return {"total": int(total or 0), "active": int(active or 0)}


class SQLAlchemyEventLog(_SQLAlchemyBackend):
    """SQLAlchemy append-only event log."""

    capabilities = BackendCapabilities({"replay", "live_tail", "delete"})

    def __init__(self, *, session_maker: Any, **kwargs: Any) -> None:
        super().__init__(session_maker=session_maker, **kwargs)
        self._condition = asyncio.Condition()

    async def append(self, stream: str, event: dict[str, Any]) -> int:
        job_id = event.get("job_id")
        async with self._session_maker() as session:
            result = await session.execute(
                select(func.max(WorkerEventRecord.position))
                .where(WorkerEventRecord.stream == stream)
            )
            max_position = result.scalar_one_or_none()
            position = -1 if max_position is None else max_position
            position += 1
            session.add(
                WorkerEventRecord(
                    stream=stream,
                    position=position,
                    job_id=str(job_id) if job_id is not None else None,
                    event=dict(event),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return await self.append(stream, event)
            async with self._condition:
                self._condition.notify_all()
            return position

    async def read(
        self, stream: str, *, from_position: int = 0, limit: int | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        async with self._session_maker() as session:
            stmt = (
                select(WorkerEventRecord)
                .where(
                    WorkerEventRecord.stream == stream,
                    WorkerEventRecord.position >= from_position,
                )
                .order_by(WorkerEventRecord.position)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return [(row.position, dict(row.event)) for row in result.scalars().all()]

    async def read_filtered(
        self,
        stream: str,
        *,
        filters: dict[str, Any],
        from_position: int = 0,
        limit: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        async with self._session_maker() as session:
            stmt = (
                select(WorkerEventRecord)
                .where(
                    WorkerEventRecord.stream == stream,
                    WorkerEventRecord.position >= from_position,
                )
                .order_by(WorkerEventRecord.position)
            )
            for key, value in filters.items():
                if key == "job_id":
                    stmt = stmt.where(WorkerEventRecord.job_id == str(value))
                else:
                    stmt = stmt.where(WorkerEventRecord.event[key].as_string() == str(value))
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return [(row.position, dict(row.event)) for row in result.scalars().all()]

    async def read_tail(self, stream: str, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerEventRecord)
                .where(WorkerEventRecord.stream == stream)
                .order_by(WorkerEventRecord.position.desc())
                .limit(limit)
            )
            events = [
                (row.position, dict(row.event))
                for row in result.scalars().all()
            ]
            return list(reversed(events))

    async def subscribe(
        self, stream: str, *, from_position: int | None = None
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        cursor = from_position
        if cursor is None:
            async with self._session_maker() as session:
                result = await session.execute(
                    select(func.max(WorkerEventRecord.position)).where(
                        WorkerEventRecord.stream == stream
                    )
                )
                max_position = result.scalar_one_or_none()
                cursor = (-1 if max_position is None else max_position) + 1
        while True:
            events = await self.read(stream, from_position=cursor, limit=50)
            if not events:
                async with self._condition:
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=0.1)
                    except TimeoutError:
                        pass
                continue
            for position, event in events:
                cursor = position + 1
                yield position, event

    async def delete(self, stream: str) -> None:
        async with self._session_maker() as session:
            await session.execute(
                delete(WorkerEventRecord).where(WorkerEventRecord.stream == stream)
            )
            await session.commit()

    async def completed_job_history(
        self,
        *,
        hours: int = 24,
        bucket_count: int = 96,
    ) -> list[dict[str, Any]]:
        cutoff = _now() - timedelta(hours=hours)
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerEventRecord.event)
                .where(WorkerEventRecord.stream == "workers:lifecycle")
                .order_by(WorkerEventRecord.position.desc())
            )
            events: list[dict[str, Any]] = []
            for event in result.scalars().all():
                if event.get("type") != "job_completed":
                    continue
                try:
                    timestamp = datetime.fromisoformat(str(event.get("timestamp", "")))
                except ValueError:
                    continue
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if timestamp < cutoff:
                    break
                events.append(dict(event))
        from skrift.workers.runtime import WorkerRuntime

        return WorkerRuntime._bucket_completed_events(
            events,
            hours=hours,
            bucket_count=bucket_count,
        )


class SQLAlchemyQueue(_SQLAlchemyBackend):
    """SQLAlchemy named queue with claim/ack semantics."""

    capabilities = BackendCapabilities(
        {"named_queues", "delayed", "visibility_timeout", "retry", "dead_letter", "inspect"}
    )

    async def submit(self, job: JobEnvelope) -> None:
        now = _now()
        visible_at = job.scheduled_for or now
        job.ready_since = visible_at if visible_at <= now else None
        async with self._session_maker() as session:
            session.add(
                WorkerQueueRecord(
                    job_id=job.id,
                    queue=job.queue,
                    job=_job_to_json(job),
                    visible_at=visible_at,
                    dead_lettered=False,
                )
            )
            await session.commit()

    async def claim(
        self, queues: list[str], *, visibility_timeout: float
    ) -> ClaimedJob | None:
        now = _now()
        await self._release_expired_claims(now)
        async with self._session_maker() as session:
            for queue in queues:
                result = await session.execute(
                    select(WorkerQueueRecord)
                    .where(
                        WorkerQueueRecord.queue == queue,
                        WorkerQueueRecord.dead_lettered.is_(False),
                        WorkerQueueRecord.claim_token.is_(None),
                        WorkerQueueRecord.visible_at <= now,
                    )
                    .order_by(WorkerQueueRecord.visible_at, WorkerQueueRecord.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    continue
                job = JobEnvelope.model_validate(record.job)
                visible_at = _utc(record.visible_at)
                if job.ready_since is None:
                    job.ready_since = visible_at
                token = uuid4().hex
                job.ready_since = None
                claimed = await session.execute(
                    update(WorkerQueueRecord)
                    .where(
                        WorkerQueueRecord.id == record.id,
                        WorkerQueueRecord.dead_lettered.is_(False),
                        WorkerQueueRecord.claim_token.is_(None),
                        WorkerQueueRecord.visible_at <= now,
                    )
                    .values(
                        job=_job_to_json(job),
                        claim_token=token,
                        claim_expires_at=now + timedelta(seconds=visibility_timeout),
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                return ClaimedJob(job=job, token=token)
            return None

    async def ack(self, queue: str, job_id: str, token: str) -> None:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerQueueRecord).where(
                    WorkerQueueRecord.queue == queue,
                    WorkerQueueRecord.job_id == job_id,
                )
            )
            record = result.scalar_one_or_none()
            if record is None or record.claim_token != token:
                raise ValueError(f"Invalid claim token for job {job_id}")
            await session.delete(record)
            await session.commit()

    async def nack(
        self,
        queue: str,
        job_id: str,
        token: str,
        *,
        retry_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> None:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerQueueRecord).where(
                    WorkerQueueRecord.queue == queue,
                    WorkerQueueRecord.job_id == job_id,
                )
            )
            record = result.scalar_one_or_none()
            if record is None or record.claim_token != token:
                raise ValueError(f"Invalid claim token for job {job_id}")
            visible_at = retry_at or _now()
            job = JobEnvelope.model_validate(record.job)
            job.ready_since = (
                visible_at if visible_at <= _now() and not dead_letter else None
            )
            record.job = _job_to_json(job)
            record.claim_token = None
            record.claim_expires_at = None
            record.dead_lettered = dead_letter
            record.visible_at = visible_at
            await session.commit()

    async def cancel(self, queue: str, job_id: str) -> bool:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerQueueRecord).where(
                    WorkerQueueRecord.queue == queue,
                    WorkerQueueRecord.job_id == job_id,
                    WorkerQueueRecord.claim_token.is_(None),
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return False
            await session.delete(record)
            await session.commit()
            return True

    async def wake(
        self, queue: str, job_id: str, *, resume_at: datetime | None = None
    ) -> bool:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerQueueRecord).where(
                    WorkerQueueRecord.queue == queue,
                    WorkerQueueRecord.job_id == job_id,
                    WorkerQueueRecord.dead_lettered.is_(False),
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return False
            visible_at = resume_at or _now()
            job = JobEnvelope.model_validate(record.job)
            job.scheduled_for = visible_at
            job.ready_since = visible_at if visible_at <= _now() else None
            record.job = _job_to_json(job)
            record.visible_at = visible_at
            await session.commit()
            return True

    async def stats(self, queue: str) -> QueueStats:
        now = _now()
        await self._release_expired_claims(now)
        stats = QueueStats(queue=queue)
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerQueueRecord).where(WorkerQueueRecord.queue == queue)
            )
            records = result.scalars().all()
            for record in records:
                visible_at = _utc(record.visible_at)
                if record.dead_lettered:
                    stats.dead_lettered += 1
                elif record.claim_token is not None:
                    stats.claimed += 1
                elif visible_at > now:
                    stats.delayed += 1
                else:
                    stats.ready += 1
                    job = JobEnvelope.model_validate(record.job)
                    if job.ready_since is None:
                        job.ready_since = visible_at
                        record.job = _job_to_json(job)
                    stats.oldest_ready_age_seconds = max(
                        stats.oldest_ready_age_seconds,
                        (now - _utc(job.ready_since)).total_seconds(),
                    )
            await session.commit()
            return stats

    async def _release_expired_claims(self, now: datetime) -> None:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerQueueRecord).where(
                    WorkerQueueRecord.dead_lettered.is_(False),
                    WorkerQueueRecord.claim_token.is_not(None),
                    WorkerQueueRecord.claim_expires_at.is_not(None),
                    WorkerQueueRecord.claim_expires_at <= now,
                )
            )
            records = result.scalars().all()
            for record in records:
                job = JobEnvelope.model_validate(record.job)
                job.reclaim_count += 1
                job.ready_since = now
                record.job = _job_to_json(job)
                record.claim_token = None
                record.claim_expires_at = None
                record.visible_at = now
            if records:
                await session.commit()


class SQLAlchemyDeadLetterStore(_SQLAlchemyBackend):
    """SQLAlchemy dead-letter store for worker forensics."""

    capabilities = BackendCapabilities({"inspect", "replay", "discard", "export"})

    async def create(self, entry: DeadJobEntry) -> DeadJobEntry:
        async with self._session_maker() as session:
            session.add(self._record_from_entry(entry))
            await session.commit()
            return entry.model_copy(deep=True)

    async def get(self, entry_id: str) -> DeadJobEntry | None:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerDeadLetterRecord).where(
                    WorkerDeadLetterRecord.entry_id == entry_id
                )
            )
            record = result.scalar_one_or_none()
            return self._entry_from_record(record) if record is not None else None

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
        async with self._session_maker() as session:
            stmt = select(WorkerDeadLetterRecord)
            if queue:
                stmt = stmt.where(WorkerDeadLetterRecord.queue == queue)
            if job_type:
                stmt = stmt.where(WorkerDeadLetterRecord.job_type == job_type)
            if cause:
                stmt = stmt.where(WorkerDeadLetterRecord.cause == cause)
            if state:
                stmt = stmt.where(WorkerDeadLetterRecord.state == state)
            if created_after:
                stmt = stmt.where(WorkerDeadLetterRecord.entry_created_at >= created_after)
            if created_before:
                stmt = stmt.where(WorkerDeadLetterRecord.entry_created_at <= created_before)
            if exception_type:
                stmt = stmt.where(
                    or_(
                        WorkerDeadLetterRecord.exception_types == exception_type,
                        WorkerDeadLetterRecord.exception_types.like(f"{exception_type},%"),
                        WorkerDeadLetterRecord.exception_types.like(f"%,{exception_type},%"),
                        WorkerDeadLetterRecord.exception_types.like(f"%,{exception_type}"),
                    )
                )
            stmt = stmt.order_by(WorkerDeadLetterRecord.entry_created_at.desc())
            result = await session.execute(stmt)
            return [
                self._entry_from_record(record)
                for record in result.scalars().all()
            ]

    async def summary(self) -> dict[str, Any]:
        async with self._session_maker() as session:
            open_count = await session.scalar(
                select(func.count()).select_from(WorkerDeadLetterRecord).where(
                    WorkerDeadLetterRecord.state == "open"
                )
            )
            last_hour_cutoff = _now() - timedelta(hours=1)
            last_hour = await session.scalar(
                select(func.count()).select_from(WorkerDeadLetterRecord).where(
                    WorkerDeadLetterRecord.state == "open",
                    WorkerDeadLetterRecord.entry_created_at >= last_hour_cutoff,
                )
            )
            top = await session.execute(
                select(WorkerDeadLetterRecord.cause, func.count())
                .where(WorkerDeadLetterRecord.state == "open")
                .group_by(WorkerDeadLetterRecord.cause)
                .order_by(func.count().desc())
                .limit(1)
            )
            top_row = top.first()
            top_cause = top_row[0] if top_row else ""
            top_cause_count = int(top_row[1]) if top_row else 0
            return {
                "open": int(open_count or 0),
                "last_hour": int(last_hour or 0),
                "top_cause": top_cause,
                "top_cause_count": top_cause_count,
            }

    async def save(self, entry: DeadJobEntry) -> DeadJobEntry:
        entry = entry.model_copy(deep=True, update={"updated_at": _now()})
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerDeadLetterRecord).where(
                    WorkerDeadLetterRecord.entry_id == entry.id
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                session.add(self._record_from_entry(entry))
            else:
                self._update_record(record, entry)
            await session.commit()
            return entry.model_copy(deep=True)

    async def prune_resolved(self, *, max_age_seconds: float) -> int:
        cutoff = _now() - timedelta(seconds=max_age_seconds)
        async with self._session_maker() as session:
            result = await session.execute(
                delete(WorkerDeadLetterRecord).where(
                    WorkerDeadLetterRecord.state != DeadLetterState.OPEN.value,
                    WorkerDeadLetterRecord.entry_updated_at <= cutoff,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    def _record_from_entry(self, entry: DeadJobEntry) -> WorkerDeadLetterRecord:
        return WorkerDeadLetterRecord(
            entry_id=entry.id,
            queue=entry.queue,
            job_type=entry.job_type,
            cause=entry.cause.value,
            state=entry.state.value,
            exception_types=self._exception_types(entry),
            entry=_entry_to_json(entry),
            entry_created_at=entry.created_at,
            entry_updated_at=entry.updated_at,
        )

    def _update_record(self, record: WorkerDeadLetterRecord, entry: DeadJobEntry) -> None:
        record.queue = entry.queue
        record.job_type = entry.job_type
        record.cause = entry.cause.value
        record.state = entry.state.value
        record.exception_types = self._exception_types(entry)
        record.entry = _entry_to_json(entry)
        record.entry_created_at = entry.created_at
        record.entry_updated_at = entry.updated_at

    @staticmethod
    def _entry_from_record(record: WorkerDeadLetterRecord) -> DeadJobEntry:
        return DeadJobEntry.model_validate(record.entry).model_copy(deep=True)

    @staticmethod
    def _exception_types(entry: DeadJobEntry) -> str:
        return ",".join(
            sorted({attempt.exception_type for attempt in entry.attempts if attempt.exception_type})
        )


class SQLAlchemyArchive(_SQLAlchemyBackend):
    """SQLAlchemy cold-storage archive backend."""

    capabilities = BackendCapabilities({"events", "snapshots", "history"})

    async def bulk_insert_events(
        self, events: list[tuple[str, int, dict[str, Any]]]
    ) -> None:
        async with self._session_maker() as session:
            for stream, position, event in events:
                session.add(
                    WorkerArchiveEventRecord(
                        stream=stream,
                        position=position,
                        event=dict(event),
                    )
                )
            await session.commit()

    async def upsert_state_snapshot(
        self, key: str, value: Any, *, timestamp: datetime | None = None
    ) -> None:
        async with self._session_maker() as session:
            session.add(
                WorkerArchiveSnapshotRecord(
                    key=key,
                    value=_value_to_json(value),
                    snapshot_at=timestamp or _now(),
                )
            )
            await session.commit()

    async def query_events(
        self,
        stream: str,
        *,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        async with self._session_maker() as session:
            stmt = (
                select(WorkerArchiveEventRecord)
                .where(
                    WorkerArchiveEventRecord.stream == stream,
                    WorkerArchiveEventRecord.position >= from_position,
                )
                .order_by(WorkerArchiveEventRecord.position)
            )
            if to_position is not None:
                stmt = stmt.where(WorkerArchiveEventRecord.position <= to_position)
            result = await session.execute(stmt)
            return [(row.position, dict(row.event)) for row in result.scalars().all()]

    async def latest_state_snapshot(self, key: str) -> Any:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerArchiveSnapshotRecord)
                .where(WorkerArchiveSnapshotRecord.key == key)
                .order_by(WorkerArchiveSnapshotRecord.snapshot_at.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            return _value_from_json(record.value) if record is not None else None

    async def historical_state_snapshots(self, key: str) -> list[tuple[datetime, Any]]:
        async with self._session_maker() as session:
            result = await session.execute(
                select(WorkerArchiveSnapshotRecord)
                .where(WorkerArchiveSnapshotRecord.key == key)
                .order_by(WorkerArchiveSnapshotRecord.snapshot_at)
            )
            return [
                (_utc(record.snapshot_at), _value_from_json(record.value))
                for record in result.scalars().all()
            ]

    async def prune(
        self,
        *,
        event_max_age_seconds: float | None = None,
        snapshot_max_age_seconds: float | None = None,
    ) -> dict[str, int]:
        counts = {"events": 0, "snapshots": 0}
        async with self._session_maker() as session:
            if event_max_age_seconds is not None:
                event_cutoff = _now() - timedelta(seconds=event_max_age_seconds)
                result = await session.execute(
                    delete(WorkerArchiveEventRecord).where(
                        WorkerArchiveEventRecord.created_at <= event_cutoff
                    )
                )
                counts["events"] = int(result.rowcount or 0)
            if snapshot_max_age_seconds is not None:
                snapshot_cutoff = _now() - timedelta(seconds=snapshot_max_age_seconds)
                result = await session.execute(
                    delete(WorkerArchiveSnapshotRecord).where(
                        WorkerArchiveSnapshotRecord.snapshot_at <= snapshot_cutoff
                    )
                )
                counts["snapshots"] = int(result.rowcount or 0)
            await session.commit()
        return counts
