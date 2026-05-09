"""Worker runtime, pools, handles, and public API helpers."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import random
import traceback
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from skrift.workers.interfaces import Archive, DeadLetterStore, EventLog, Queue, StateStore
from skrift.workers.memory import (
    InMemoryArchive,
    InMemoryDeadLetterStore,
    InMemoryEventLog,
    InMemoryQueue,
    InMemoryStateStore,
)
from skrift.workers.models import (
    ClaimedJob,
    DeadJobAttempt,
    DeadJobEntry,
    DeadLetterCause,
    DeadLetterState,
    JobEnvelope,
    JobIdConflict,
    JobState,
    JobStatus,
    LifecycleEventType,
    Pause,
    RetryPolicy,
    WorkerLifecycleEvent,
    utcnow,
)
from skrift.workers.registry import HandlerDescriptor, HandlerRegistry, registry


LIFECYCLE_STREAM = "workers:lifecycle"
QUEUE_WAIT_HISTORY_STATE_KEY = "workers:queue_wait_history"
QUEUE_TREND_HISTORY_STATE_KEY = "workers:queue_trend_history"
ExecutionMode = Literal["inline", "in_process", "out_of_process"]
logger = logging.getLogger(__name__)


class JobFailed(RuntimeError):
    """Raised when awaiting a failed worker job."""


class PermanentFailure(RuntimeError):
    """Raised by handlers to skip remaining retries and dead-letter immediately."""


class JobCancelled(asyncio.CancelledError):
    """Raised when awaiting a cancelled worker job."""


@dataclass(frozen=True)
class WorkerConfig:
    """Runtime settings for the MVP local worker executor."""

    mode: ExecutionMode = "inline"
    queues: tuple[str, ...] = ("default",)
    concurrency: int = 1
    poll_interval: float = 0.05
    visibility_timeout: float = 30.0
    max_reclaims: int = 3


@dataclass(frozen=True)
class WorkerBackendConfig:
    """Import paths for backend implementations."""

    state_store: str = "skrift.workers.memory:InMemoryStateStore"
    event_log: str = "skrift.workers.memory:InMemoryEventLog"
    queue: str = "skrift.workers.memory:InMemoryQueue"
    dead_letter_store: str = "skrift.workers.memory:InMemoryDeadLetterStore"
    archive: str = "skrift.workers.memory:InMemoryArchive"


@dataclass
class WorkerContext:
    """Context passed to handlers that accept a second argument."""

    runtime: "WorkerRuntime"
    job: JobEnvelope
    paused_state: dict[str, Any]

    async def emit(self, stream: str, event: dict[str, Any]) -> int:
        return await self.runtime.event_log.append(stream, event)


class JobHandle:
    """Awaitable/queryable handle returned by `submit`."""

    def __init__(self, runtime: "WorkerRuntime", job_id: str) -> None:
        self._runtime = runtime
        self.id = job_id

    def __await__(self):
        return self.result().__await__()

    async def status(self) -> JobState:
        state = await self._runtime.get_job_state(self.id)
        if state is None:
            raise KeyError(f"Unknown worker job id {self.id!r}")
        return state

    async def result(self, *, timeout: float | None = None) -> Any:
        return await self._runtime.wait_for_result(self.id, timeout=timeout)

    async def cancel(self) -> bool:
        return await self._runtime.cancel(self.id)


class WorkerPool:
    """Runs N concurrent in-process worker loops."""

    def __init__(
        self,
        runtime: "WorkerRuntime",
        *,
        queues: list[str],
        concurrency: int = 1,
        poll_interval: float = 0.05,
    ) -> None:
        self._runtime = runtime
        self._queues = queues
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._run_worker(), name=f"skrift-worker-{i}")
            for i in range(self._concurrency)
        ]

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run_worker(self) -> None:
        while not self._stopping.is_set():
            try:
                claimed = await self._runtime.queue.claim(
                    self._queues, visibility_timeout=self._runtime.default_visibility_timeout
                )
                if claimed is None:
                    await asyncio.sleep(self._poll_interval)
                    continue
                await self._runtime.execute_claim(claimed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Worker loop error; continuing", exc_info=True)
                await asyncio.sleep(self._poll_interval)


class WorkerRuntime:
    """Coordinates local worker backends and execution."""

    def __init__(
        self,
        *,
        config: WorkerConfig | None = None,
        state_store: StateStore | None = None,
        event_log: EventLog | None = None,
        queue: Queue | None = None,
        dead_letter_store: DeadLetterStore | None = None,
        archive: Archive | None = None,
        handler_registry: HandlerRegistry | None = None,
    ) -> None:
        self.config = config or WorkerConfig()
        self.state_store = state_store or InMemoryStateStore()
        self.event_log = event_log or InMemoryEventLog()
        self.queue = queue or InMemoryQueue()
        self.dead_letter_store = dead_letter_store or InMemoryDeadLetterStore()
        self.archive = archive or InMemoryArchive()
        self.registry = handler_registry or registry
        self.default_visibility_timeout = self.config.visibility_timeout
        self._condition = asyncio.Condition()
        self._pool: WorkerPool | None = None
        self._queue_history_retention = timedelta(hours=24)
        self._queue_history_bucket_count = 96
        self._queue_history_bucket_seconds = int(
            self._queue_history_retention.total_seconds()
            / self._queue_history_bucket_count
        )
        self._queue_history: deque[dict[str, Any]] = deque(
            maxlen=self._queue_history_bucket_count
        )
        self._queue_trend_sample_count = 180
        self._queue_trend_history: deque[dict[str, Any]] = deque(
            maxlen=self._queue_trend_sample_count
        )
        self._queue_history_lock = asyncio.Lock()
        self._queue_history_task: asyncio.Task | None = None
        self._queue_history_interval = 2.0
        self._queue_trend_bucket_seconds = self._queue_history_interval

    async def start(self) -> None:
        if self.config.mode != "in_process":
            return
        await self.record_queue_history()
        self._pool = WorkerPool(
            self,
            queues=list(self.config.queues),
            concurrency=self.config.concurrency,
            poll_interval=self.config.poll_interval,
        )
        await self._pool.start()
        if self._queue_history_task is None:
            self._queue_history_task = asyncio.create_task(
                self._record_queue_history_loop(),
                name="skrift-worker-queue-history",
            )

    async def stop(self) -> None:
        if self._queue_history_task is not None:
            self._queue_history_task.cancel()
            await asyncio.gather(self._queue_history_task, return_exceptions=True)
            self._queue_history_task = None
        if self._pool is not None:
            await self._pool.stop()
            self._pool = None

    async def submit(
        self,
        job_or_type: BaseModel | str,
        payload: BaseModel | dict[str, Any] | None = None,
        *,
        queue: str | None = None,
        retry_policy: RetryPolicy | None = None,
        scheduled_for: datetime | None = None,
        correlation_id: str | None = None,
        parent_job_id: str | None = None,
        visibility_timeout: float | None = None,
        job_id: str | None = None,
    ) -> JobHandle:
        try:
            job_type, descriptor, payload_model = self._resolve_submission(job_or_type, payload)
        except ValidationError as exc:
            job_type, descriptor, raw_payload = self._poison_submission(job_or_type, payload)
            job = self._build_job(
                job_type,
                descriptor,
                raw_payload,
                queue=queue,
                retry_policy=retry_policy,
                scheduled_for=scheduled_for,
                correlation_id=correlation_id,
                parent_job_id=parent_job_id,
                visibility_timeout=visibility_timeout,
                job_id=job_id,
            )
            attempt = self._attempt_from_exception(job, exc, started_at=utcnow())
            await self._dead_letter(
                job,
                cause=DeadLetterCause.POISON,
                attempts=[attempt],
                error=f"{type(exc).__name__}: {exc}",
            )
            return JobHandle(self, job.id)

        payload_data = payload_model.model_dump(mode="json")
        job = self._build_job(
            job_type,
            descriptor,
            payload_data,
            queue=queue,
            retry_policy=retry_policy,
            scheduled_for=scheduled_for,
            correlation_id=correlation_id,
            parent_job_id=parent_job_id,
            visibility_timeout=visibility_timeout,
            job_id=job_id,
        )
        existing_state = await self.get_job_state(job.id)
        if existing_state is not None:
            if self._same_idempotent_job(existing_state.job, job):
                return JobHandle(self, job.id)
            raise JobIdConflict(f"job id {job.id!r} already exists")
        await self._set_state(JobState(job=job, status=JobStatus.SUBMITTED))
        await self.emit_lifecycle(LifecycleEventType.JOB_SUBMITTED, job)
        handle = JobHandle(self, job.id)
        if self.config.mode == "inline":
            claimed = ClaimedJob(job=job, token="inline")
            await self.execute_claim(claimed, inline=True)
        elif self.config.mode in {"in_process", "out_of_process"}:
            await self.queue.submit(job, job_id=job.id)
        else:
            raise NotImplementedError(f"Unsupported worker execution mode {self.config.mode!r}")
        return handle

    def _build_job(
        self,
        job_type: str,
        descriptor: HandlerDescriptor,
        payload_data: dict[str, Any],
        *,
        queue: str | None,
        retry_policy: RetryPolicy | None,
        scheduled_for: datetime | None,
        correlation_id: str | None,
        parent_job_id: str | None,
        visibility_timeout: float | None,
        replayed_from: str | None = None,
        job_id: str | None = None,
    ) -> JobEnvelope:
        policy = retry_policy or descriptor.retry_policy
        return JobEnvelope(
            id=job_id or JobEnvelope.model_fields["id"].default_factory(),
            type=job_type,
            queue=queue or descriptor.queue,
            payload=payload_data,
            max_attempts=policy.max_attempts,
            visibility_timeout=visibility_timeout or descriptor.visibility_timeout,
            max_reclaims=self.config.max_reclaims,
            scheduled_for=scheduled_for,
            correlation_id=correlation_id,
            parent_job_id=parent_job_id,
            replayed_from=replayed_from,
        )

    async def get_job_state(self, job_id: str) -> JobState | None:
        return await self.state_store.get(self._job_key(job_id))

    async def wait_for_result(self, job_id: str, *, timeout: float | None = None) -> Any:
        async def _wait() -> Any:
            while True:
                state = await self.get_job_state(job_id)
                if state is None:
                    raise KeyError(f"Unknown worker job id {job_id!r}")
                if state.status == JobStatus.COMPLETED:
                    return state.result
                if state.status in {JobStatus.FAILED, JobStatus.DEAD_LETTERED}:
                    raise JobFailed(state.error or state.last_error or f"Job {job_id} failed")
                if state.status == JobStatus.CANCELLED:
                    raise JobCancelled(f"Job {job_id} was cancelled")
                async with self._condition:
                    await self._condition.wait()

        if timeout is None:
            return await _wait()
        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def cancel(self, job_id: str) -> bool:
        state = await self.get_job_state(job_id)
        if state is None or state.status != JobStatus.SUBMITTED:
            return False
        cancelled = await self.queue.cancel(state.job.queue, job_id)
        if not cancelled and self.config.mode != "inline":
            return False
        await self._set_state(state.model_copy(update={"status": JobStatus.CANCELLED}))
        await self.emit_lifecycle(LifecycleEventType.JOB_CANCELLED, state.job)
        return True

    async def wake(self, job_id: str, *, resume_at: datetime | None = None) -> bool:
        state = await self.get_job_state(job_id)
        if state is None:
            return False
        return await self.queue.wake(state.job.queue, job_id, resume_at=resume_at)

    async def inspect(
        self,
        *,
        queue_names: list[str] | None = None,
        job_limit: int | None = None,
        event_limit: int = 25,
    ) -> dict[str, Any]:
        """Return a read-only snapshot for admin/operator views."""
        states, total_jobs = await self._job_states_with_total(limit=job_limit)
        active_jobs = await self._active_job_count(states, total_jobs=total_jobs)
        states.sort(key=lambda state: state.updated_at, reverse=True)

        queues = self._queue_names(states, queue_names=queue_names)
        queue_stats = [await self.queue.stats(queue) for queue in queues]
        await self.record_queue_history(queue_stats=queue_stats)
        lifecycle_events = await self._lifecycle_events(limit=event_limit)

        return {
            "mode": self.config.mode,
            "concurrency": self.config.concurrency,
            "queues": queue_stats,
            "queue_trend_history": await self.queue_trend_history(),
            "queue_wait_history": await self.queue_wait_history(),
            "queue_wait_bucket_seconds": self._queue_history_bucket_seconds,
            "completed_history": await self.completed_job_history(),
            "dlq": await self.dlq_summary(),
            "jobs": states,
            "jobs_total": total_jobs,
            "jobs_active_total": active_jobs,
            "jobs_limit": job_limit,
            "handlers": self.registry.list_handlers(),
            "events": list(reversed(lifecycle_events[-event_limit:])),
        }

    async def dlq_summary(self) -> dict[str, Any]:
        summary = getattr(self.dead_letter_store, "summary", None)
        if callable(summary):
            return await summary()
        entries = await self.dead_letter_store.list()
        open_entries = [entry for entry in entries if entry.state == DeadLetterState.OPEN]
        last_hour_cutoff = utcnow() - timedelta(hours=1)
        recent = [entry for entry in open_entries if entry.created_at >= last_hour_cutoff]
        counts: dict[str, int] = {}
        for entry in open_entries:
            counts[entry.cause.value] = counts.get(entry.cause.value, 0) + 1
        top_cause = max(counts, key=counts.get) if counts else ""
        return {
            "open": len(open_entries),
            "last_hour": len(recent),
            "top_cause": top_cause,
            "top_cause_count": counts.get(top_cause, 0) if top_cause else 0,
        }

    async def completed_job_history(
        self,
        *,
        hours: int = 24,
        bucket_count: int = 96,
    ) -> list[dict[str, Any]]:
        history = getattr(self.event_log, "completed_job_history", None)
        if callable(history):
            return await history(hours=hours, bucket_count=bucket_count)
        events = await self.event_log.read(LIFECYCLE_STREAM)
        return self._bucket_completed_events(
            [event for _, event in events],
            hours=hours,
            bucket_count=bucket_count,
        )

    async def inspect_dlq(self, **filters: Any) -> list[DeadJobEntry]:
        """Return filtered DLQ entries for admin/operator views."""
        return await self.dead_letter_store.list(**filters)

    async def get_dlq_entry(self, entry_id: str) -> DeadJobEntry | None:
        """Return one DLQ entry."""
        return await self.dead_letter_store.get(entry_id)

    async def retry_dlq_entry(
        self,
        entry_id: str,
        *,
        force: bool = False,
        scheduled_for: datetime | None = None,
    ) -> JobHandle:
        """Replay a DLQ entry as a new job with clean retry state."""
        entry = await self.dead_letter_store.get(entry_id)
        if entry is None:
            raise KeyError(f"Unknown DLQ entry id {entry_id!r}")
        if entry.cause == DeadLetterCause.PERMANENT_FAILURE and not force:
            raise PermissionError("Permanent failures require force retry")
        if entry.cause == DeadLetterCause.POISON and not force:
            raise PermissionError("Poison jobs require force retry")
        descriptor = self.registry.get(entry.job_type)
        job = self._build_job(
            entry.job_type,
            descriptor,
            dict(entry.job.payload),
            queue=entry.queue,
            retry_policy=descriptor.retry_policy,
            scheduled_for=scheduled_for,
            correlation_id=entry.job.correlation_id,
            parent_job_id=entry.job.id,
            visibility_timeout=entry.job.visibility_timeout,
            replayed_from=entry.id,
        )
        await self._set_state(JobState(job=job, status=JobStatus.SUBMITTED))
        await self.emit_lifecycle(LifecycleEventType.JOB_SUBMITTED, job)
        if self.config.mode == "inline":
            await self.execute_claim(ClaimedJob(job=job, token="inline"), inline=True)
        else:
            await self.queue.submit(job, job_id=job.id)
            await self.queue.cancel(entry.queue, entry.job.id)
        entry.state = DeadLetterState.REPLAYED
        entry.replayed_to_job_id = job.id
        entry.replayed_at = utcnow()
        await self.dead_letter_store.save(entry)
        return JobHandle(self, job.id)

    async def discard_dlq_entry(
        self,
        entry_id: str,
        *,
        reason: str | None = None,
    ) -> DeadJobEntry:
        """Mark a DLQ entry as discarded without deleting forensic data."""
        entry = await self.dead_letter_store.get(entry_id)
        if entry is None:
            raise KeyError(f"Unknown DLQ entry id {entry_id!r}")
        await self.queue.cancel(entry.queue, entry.job.id)
        entry.state = DeadLetterState.DISCARDED
        entry.discarded_reason = reason
        entry.discarded_at = utcnow()
        return await self.dead_letter_store.save(entry)

    async def retry_dlq_entries(
        self,
        entry_ids: list[str],
        *,
        force: bool = False,
    ) -> list[JobHandle]:
        return [
            await self.retry_dlq_entry(entry_id, force=force)
            for entry_id in entry_ids
        ]

    async def discard_dlq_entries(
        self,
        entry_ids: list[str],
        *,
        reason: str | None = None,
    ) -> list[DeadJobEntry]:
        return [
            await self.discard_dlq_entry(entry_id, reason=reason)
            for entry_id in entry_ids
        ]

    async def queue_wait_history(self) -> list[dict[str, Any]]:
        """Return stored oldest-ready wait samples for operator graphs."""
        async with self._queue_history_lock:
            await self._load_queue_history_locked()
            await self._load_queue_trend_history_locked()
            self._prune_queue_history_locked(utcnow())
            self._prune_queue_trend_history_locked()
            await self._persist_queue_history_locked()
            await self._persist_queue_trend_history_locked()
            return [
                {
                    "timestamp": sample["timestamp"],
                    "queues": [dict(queue) for queue in sample["queues"]],
                }
                for sample in self._queue_history
            ]

    async def queue_trend_history(self) -> list[dict[str, Any]]:
        """Return recent queue count samples using the live chart cadence."""
        async with self._queue_history_lock:
            await self._load_queue_trend_history_locked()
            self._prune_queue_trend_history_locked()
            await self._persist_queue_trend_history_locked()
            return [
                {
                    "timestamp": sample["timestamp"],
                    "queues": [dict(queue) for queue in sample["queues"]],
                }
                for sample in self._queue_trend_history
            ]

    async def record_queue_history(
        self,
        *,
        queue_stats: list[Any] | None = None,
    ) -> None:
        """Store one compressed oldest-ready wait bucket for all known queues."""
        timestamp = utcnow()

        if queue_stats is None:
            states, _ = await self._job_states_with_total(limit=100)
            queue_stats = [
                await self.queue.stats(queue)
                for queue in self._queue_names(states)
            ]

        bucket_start = self._queue_history_bucket_start(timestamp)
        sample = {
            "recorded_at": bucket_start,
            "timestamp": bucket_start.isoformat(),
            "queues": [
                {
                    "queue": stats.queue,
                    "ready": stats.ready,
                    "delayed": stats.delayed,
                    "claimed": stats.claimed,
                    "dead_lettered": stats.dead_lettered,
                    "oldest_ready_age_seconds": round(
                        stats.oldest_ready_age_seconds,
                        3,
                    ),
                }
                for stats in queue_stats
            ],
        }
        trend_bucket_start = self._queue_trend_bucket_start(timestamp)
        trend_sample = {
            "recorded_at": trend_bucket_start,
            "timestamp": trend_bucket_start.isoformat(),
            "queues": [dict(queue) for queue in sample["queues"]],
        }
        async with self._queue_history_lock:
            await self._load_queue_history_locked()
            await self._load_queue_trend_history_locked()
            self._prune_queue_history_locked(timestamp)
            self._record_queue_trend_sample_locked(trend_sample)
            for index, existing in enumerate(self._queue_history):
                if existing["recorded_at"] == bucket_start:
                    self._queue_history[index] = self._merge_queue_history_sample(
                        existing,
                        sample,
                    )
                    await self._persist_queue_history_locked()
                    await self._persist_queue_trend_history_locked()
                    return
            self._queue_history.append(sample)
            await self._persist_queue_history_locked()
            await self._persist_queue_trend_history_locked()

    async def execute_claim(self, claimed: ClaimedJob, *, inline: bool = False) -> None:
        job = claimed.job
        if job.reclaim_count >= job.max_reclaims:
            await self._dead_letter_claim(
                claimed,
                cause=DeadLetterCause.RECLAIM_LOOP,
                error=f"Job reclaimed {job.reclaim_count} times",
                inline=inline,
            )
            return
        descriptor = self.registry.get(job.type)
        job.attempt += 1
        started_at = utcnow()
        previous_state = await self.get_job_state(job.id)
        await self.emit_lifecycle(LifecycleEventType.JOB_CLAIMED, job)
        if previous_state is not None and previous_state.status == JobStatus.PAUSED:
            await self.emit_lifecycle(LifecycleEventType.JOB_RESUMED, job)
        await self._set_state(
            JobState(
                job=job,
                status=JobStatus.RUNNING,
                attempt=job.attempt,
                paused_state=previous_state.paused_state if previous_state is not None else {},
            )
        )
        await self.emit_lifecycle(LifecycleEventType.JOB_STARTED, job)
        try:
            result = await self._call_handler(descriptor, job)
        except PermanentFailure as exc:
            await self._handle_failure(
                claimed,
                descriptor.retry_policy,
                exc,
                inline=inline,
                started_at=started_at,
                permanent=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            await self._handle_failure(
                claimed,
                descriptor.retry_policy,
                exc,
                inline=inline,
                started_at=started_at,
            )
            return

        if isinstance(result, Pause):
            await self._handle_pause(claimed, result, inline=inline)
            return

        if not inline:
            await self.queue.ack(job.queue, job.id, claimed.token)
        await self._set_state(
            JobState(
                job=job,
                status=JobStatus.COMPLETED,
                attempt=job.attempt,
                result=result,
            )
        )
        await self.emit_lifecycle(LifecycleEventType.JOB_COMPLETED, job)

    async def emit_lifecycle(
        self,
        event_type: LifecycleEventType,
        job: JobEnvelope,
        *,
        error: str | None = None,
    ) -> None:
        event = WorkerLifecycleEvent(
            type=event_type,
            job_id=job.id,
            queue=job.queue,
            job_type=job.type,
            attempt=job.attempt,
            error=error,
        )
        await self.event_log.append(LIFECYCLE_STREAM, event.model_dump(mode="json"))

    def handle(self, job_id: str) -> JobHandle:
        return JobHandle(self, job_id)

    def _resolve_submission(
        self,
        job_or_type: BaseModel | str,
        payload: BaseModel | dict[str, Any] | None,
    ) -> tuple[str, HandlerDescriptor, BaseModel]:
        if isinstance(job_or_type, str):
            descriptor = self.registry.get(job_or_type)
            if payload is None:
                raise TypeError("submit(job_type, payload) requires a payload")
            model = descriptor.payload_model.model_validate(payload)
            return job_or_type, descriptor, model

        job_type = self.registry.job_type_for_payload(job_or_type)
        descriptor = self.registry.get(job_type)
        return job_type, descriptor, descriptor.payload_model.model_validate(job_or_type)

    def _poison_submission(
        self,
        job_or_type: BaseModel | str,
        payload: BaseModel | dict[str, Any] | None,
    ) -> tuple[str, HandlerDescriptor, dict[str, Any]]:
        if not isinstance(job_or_type, str):
            raise TypeError("Only string job submissions can be retained as poison payloads")
        descriptor = self.registry.get(job_or_type)
        raw_payload = payload if isinstance(payload, dict) else {"value": payload}
        return job_or_type, descriptor, raw_payload

    async def _call_handler(self, descriptor: HandlerDescriptor, job: JobEnvelope) -> Any:
        payload = descriptor.payload_model.model_validate(job.payload)
        state = await self.get_job_state(job.id)
        context = WorkerContext(
            runtime=self,
            job=job,
            paused_state=state.paused_state if state is not None else {},
        )
        signature = inspect.signature(descriptor.func)
        if len(signature.parameters) >= 2:
            result = descriptor.func(payload, context)
        else:
            result = descriptor.func(payload)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _handle_failure(
        self,
        claimed: ClaimedJob,
        retry_policy: RetryPolicy,
        exc: Exception,
        *,
        inline: bool,
        started_at: datetime,
        permanent: bool = False,
    ) -> None:
        job = claimed.job
        error = f"{type(exc).__name__}: {exc}"
        attempt = self._attempt_from_exception(
            job,
            exc,
            started_at=started_at,
            claimed_at=claimed.claimed_at,
        )
        previous_state = await self.get_job_state(job.id)
        attempts = [*(previous_state.attempt_history if previous_state else []), attempt]
        await self.emit_lifecycle(LifecycleEventType.JOB_FAILED, job, error=error)
        if permanent or job.attempt >= job.max_attempts:
            if not inline:
                await self.queue.nack(job.queue, job.id, claimed.token, dead_letter=True)
            await self._dead_letter(
                job,
                cause=(
                    DeadLetterCause.PERMANENT_FAILURE
                    if permanent
                    else DeadLetterCause.RETRIES_EXHAUSTED
                ),
                attempts=attempts,
                error=error,
            )
            return

        retry_at = utcnow() + timedelta(seconds=self._retry_delay(retry_policy, job.attempt))
        await self._set_state(
            JobState(
                job=job,
                status=JobStatus.SUBMITTED,
                attempt=job.attempt,
                last_error=error,
                attempt_history=attempts,
            )
        )
        if inline:
            await self.execute_claim(ClaimedJob(job=job, token="inline"), inline=True)
        else:
            await self.queue.nack(job.queue, job.id, claimed.token, retry_at=retry_at)

    async def _handle_pause(self, claimed: ClaimedJob, pause: Pause, *, inline: bool) -> None:
        job = claimed.job
        job.attempt = max(0, job.attempt - 1)
        job.scheduled_for = pause.resume_at
        await self._set_state(
            JobState(
                job=job,
                status=JobStatus.PAUSED,
                attempt=job.attempt,
                paused_state=pause.state,
            )
        )
        await self.emit_lifecycle(LifecycleEventType.JOB_PAUSED, job)
        if inline:
            if pause.resume_at is None:
                return
            delay = max(0.0, (pause.resume_at - utcnow()).total_seconds())
            if delay:
                await asyncio.sleep(delay)
            await self.emit_lifecycle(LifecycleEventType.JOB_RESUMED, job)
            await self.execute_claim(ClaimedJob(job=job, token="inline"), inline=True)
            return
        retry_at = pause.resume_at or datetime.max.replace(tzinfo=utcnow().tzinfo)
        await self.queue.nack(job.queue, job.id, claimed.token, retry_at=retry_at)

    def _retry_delay(self, retry_policy: RetryPolicy, attempt: int) -> float:
        delay = retry_policy.backoff_seconds * max(0, attempt - 1)
        if retry_policy.jitter_seconds:
            delay += random.uniform(0, retry_policy.jitter_seconds)
        return delay

    def _attempt_from_exception(
        self,
        job: JobEnvelope,
        exc: Exception,
        *,
        started_at: datetime,
        claimed_at: datetime | None = None,
    ) -> DeadJobAttempt:
        finished_at = utcnow()
        task = asyncio.current_task()
        return DeadJobAttempt(
            attempt=max(job.attempt, 1),
            claimed_at=claimed_at,
            started_at=started_at,
            finished_at=finished_at,
            worker_id=task.get_name() if task is not None else None,
            duration_seconds=(finished_at - started_at).total_seconds(),
            exception_type=type(exc).__name__,
            error=str(exc),
            traceback="".join(traceback.format_exception(exc)),
        )

    async def _dead_letter_claim(
        self,
        claimed: ClaimedJob,
        *,
        cause: DeadLetterCause,
        error: str,
        inline: bool,
    ) -> None:
        job = claimed.job
        if not inline:
            await self.queue.nack(job.queue, job.id, claimed.token, dead_letter=True)
        await self._dead_letter(job, cause=cause, attempts=[], error=error)

    async def _dead_letter(
        self,
        job: JobEnvelope,
        *,
        cause: DeadLetterCause,
        attempts: list[DeadJobAttempt],
        error: str,
    ) -> DeadJobEntry:
        entry = DeadJobEntry(
            job=job.model_copy(deep=True),
            queue=job.queue,
            job_type=job.type,
            cause=cause,
            attempts=attempts,
            latest_error=error,
            retention_until=utcnow() + timedelta(days=30),
        )
        entry = await self.dead_letter_store.create(entry)
        await self._set_state(
            JobState(
                job=job,
                status=JobStatus.DEAD_LETTERED,
                attempt=job.attempt,
                error=error,
                last_error=error,
                attempt_history=attempts,
            )
        )
        await self.emit_lifecycle(LifecycleEventType.JOB_DEAD_LETTERED, job, error=error)
        descriptor = self.registry.get(job.type)
        if descriptor.dead_callback is not None:
            await self._call_dead_callback(descriptor, entry)
        return entry

    async def _call_dead_callback(
        self,
        descriptor: HandlerDescriptor,
        entry: DeadJobEntry,
    ) -> None:
        result = descriptor.dead_callback(entry)
        if inspect.isawaitable(result):
            await result

    async def _record_queue_history_loop(self) -> None:
        while True:
            await asyncio.sleep(self._queue_history_interval)
            await self.record_queue_history()

    async def _job_states(self) -> list[JobState]:
        states, _ = await self._job_states_with_total()
        return states

    async def _job_states_with_total(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[list[JobState], int]:
        job_states = getattr(self.state_store, "worker_job_states", None)
        if callable(job_states):
            return await job_states(limit=limit)
        keys = await self.state_store.keys("workers:jobs:")
        states = [
            state
            for key in keys
            if (state := await self.state_store.get(key)) is not None
        ]
        states.sort(key=lambda state: state.updated_at, reverse=True)
        return states[:limit] if limit is not None else states, len(states)

    async def _active_job_count(self, states: list[JobState], *, total_jobs: int) -> int:
        active_statuses = {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.PAUSED}
        counts = getattr(self.state_store, "worker_job_counts", None)
        if callable(counts):
            return int((await counts()).get("active", 0))
        if len(states) == total_jobs:
            return sum(state.status in active_statuses for state in states)
        all_states, _ = await self._job_states_with_total()
        return sum(state.status in active_statuses for state in all_states)

    async def _lifecycle_events(self, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
        if limit <= 0:
            return []
        read_tail = getattr(self.event_log, "read_tail", None)
        if callable(read_tail):
            return await read_tail(LIFECYCLE_STREAM, limit=limit)
        lifecycle_events = await self.event_log.read(LIFECYCLE_STREAM)
        return lifecycle_events[-limit:]

    async def lifecycle_events_for_job(self, job_id: str) -> list[tuple[int, dict[str, Any]]]:
        read_filtered = getattr(self.event_log, "read_filtered", None)
        if callable(read_filtered):
            return await read_filtered(
                LIFECYCLE_STREAM,
                filters={"job_id": job_id},
            )
        return [
            (position, event)
            for position, event in await self.event_log.read(LIFECYCLE_STREAM)
            if event.get("job_id") == job_id
        ]

    @staticmethod
    def _bucket_completed_events(
        events: list[dict[str, Any]],
        *,
        hours: int,
        bucket_count: int,
    ) -> list[dict[str, Any]]:
        now = utcnow()
        window = timedelta(hours=hours)
        bucket_seconds = window.total_seconds() / bucket_count
        start = now - window
        buckets = [
            {
                "timestamp": (start + timedelta(seconds=index * bucket_seconds)).isoformat(),
                "queues": {},
                "total": 0,
            }
            for index in range(bucket_count)
        ]
        for event in events:
            if event.get("type") != LifecycleEventType.JOB_COMPLETED.value:
                continue
            try:
                timestamp = datetime.fromisoformat(str(event.get("timestamp", "")))
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=now.tzinfo)
            offset = (timestamp - start).total_seconds()
            if offset < 0 or offset > window.total_seconds():
                continue
            index = min(bucket_count - 1, int(offset // bucket_seconds))
            queue = str(event.get("queue", "") or "default")
            buckets[index]["total"] += 1
            buckets[index]["queues"][queue] = buckets[index]["queues"].get(queue, 0) + 1
        return buckets

    def _queue_names(
        self,
        states: list[JobState],
        *,
        queue_names: list[str] | None = None,
    ) -> list[str]:
        if queue_names is not None:
            return sorted(queue_names or ["default"])
        queues = {state.job.queue for state in states}
        queues.update(self.config.queues)
        queues.update(handler.queue for handler in self.registry.list_handlers())
        return sorted(queues or {"default"})

    def _prune_queue_history_locked(self, now: datetime) -> None:
        cutoff = now - self._queue_history_retention
        while self._queue_history and self._queue_history[0]["recorded_at"] < cutoff:
            self._queue_history.popleft()

    def _prune_queue_trend_history_locked(self) -> None:
        while len(self._queue_trend_history) > self._queue_trend_sample_count:
            self._queue_trend_history.popleft()

    async def _load_queue_history_locked(self) -> None:
        stored = await self.state_store.get(QUEUE_WAIT_HISTORY_STATE_KEY)
        if not isinstance(stored, list):
            return
        samples = [
            sample
            for item in stored
            if (sample := self._queue_history_sample_from_storage(item)) is not None
        ]
        self._queue_history = deque(
            samples[-self._queue_history_bucket_count:],
            maxlen=self._queue_history_bucket_count,
        )

    async def _load_queue_trend_history_locked(self) -> None:
        stored = await self.state_store.get(QUEUE_TREND_HISTORY_STATE_KEY)
        if not isinstance(stored, list):
            return
        samples = [
            sample
            for item in stored
            if (sample := self._queue_history_sample_from_storage(item)) is not None
        ]
        self._queue_trend_history = deque(
            samples[-self._queue_trend_sample_count:],
            maxlen=self._queue_trend_sample_count,
        )

    async def _persist_queue_history_locked(self) -> None:
        await self.state_store.set(
            QUEUE_WAIT_HISTORY_STATE_KEY,
            [
                {
                    "timestamp": sample["timestamp"],
                    "queues": [dict(queue) for queue in sample["queues"]],
                }
                for sample in self._queue_history
            ],
        )

    async def _persist_queue_trend_history_locked(self) -> None:
        await self.state_store.set(
            QUEUE_TREND_HISTORY_STATE_KEY,
            [
                {
                    "timestamp": sample["timestamp"],
                    "queues": [dict(queue) for queue in sample["queues"]],
                }
                for sample in self._queue_trend_history
            ],
        )

    def _queue_history_bucket_start(self, timestamp: datetime) -> datetime:
        seconds = int(timestamp.timestamp())
        bucket_seconds = seconds - (seconds % self._queue_history_bucket_seconds)
        return datetime.fromtimestamp(bucket_seconds, tz=timestamp.tzinfo)

    def _queue_trend_bucket_start(self, timestamp: datetime) -> datetime:
        milliseconds = int(timestamp.timestamp() * 1000)
        bucket_ms = int(self._queue_trend_bucket_seconds * 1000)
        bucket_start_ms = milliseconds - (milliseconds % bucket_ms)
        return datetime.fromtimestamp(bucket_start_ms / 1000, tz=timestamp.tzinfo)

    @staticmethod
    def _queue_history_sample_from_storage(sample: Any) -> dict[str, Any] | None:
        if not isinstance(sample, dict):
            return None
        try:
            recorded_at = datetime.fromisoformat(str(sample.get("timestamp", "")))
        except ValueError:
            return None
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=utcnow().tzinfo)
        queues = [
            {
                "queue": str(queue.get("queue", "default") or "default"),
                "ready": int(queue.get("ready", 0) or 0),
                "delayed": int(queue.get("delayed", 0) or 0),
                "claimed": int(queue.get("claimed", 0) or 0),
                "dead_lettered": int(queue.get("dead_lettered", 0) or 0),
                "oldest_ready_age_seconds": float(
                    queue.get("oldest_ready_age_seconds", 0) or 0
                ),
            }
            for queue in sample.get("queues", [])
            if isinstance(queue, dict)
        ]
        return {
            "recorded_at": recorded_at,
            "timestamp": recorded_at.isoformat(),
            "queues": sorted(queues, key=lambda queue: queue["queue"]),
        }

    def _record_queue_trend_sample_locked(self, sample: dict[str, Any]) -> None:
        if self._queue_trend_history:
            previous = self._queue_trend_history[-1]
            if sample["recorded_at"] == previous["recorded_at"]:
                self._queue_trend_history[-1] = self._merge_queue_trend_sample(
                    previous,
                    sample,
                )
                return
        self._queue_trend_history.append(sample)

    @staticmethod
    def _merge_queue_trend_sample(
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        queues = {queue["queue"]: dict(queue) for queue in current["queues"]}
        for queue in incoming["queues"]:
            existing = queues.get(queue["queue"])
            if existing is None:
                queues[queue["queue"]] = dict(queue)
                continue
            for key in (
                "ready",
                "delayed",
                "claimed",
                "dead_lettered",
                "oldest_ready_age_seconds",
            ):
                existing[key] = max(
                    float(existing.get(key, 0) or 0),
                    float(queue.get(key, 0) or 0),
                )
            for key in ("ready", "delayed", "claimed", "dead_lettered"):
                existing[key] = int(existing[key])
        return {
            "recorded_at": current["recorded_at"],
            "timestamp": current["timestamp"],
            "queues": sorted(queues.values(), key=lambda queue: queue["queue"]),
        }

    @staticmethod
    def _merge_queue_history_sample(
        current: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        queues = {queue["queue"]: dict(queue) for queue in current["queues"]}
        for queue in incoming["queues"]:
            existing = queues.get(queue["queue"])
            if existing is None:
                queues[queue["queue"]] = dict(queue)
                continue
            existing["ready"] = queue.get("ready", 0)
            existing["delayed"] = queue.get("delayed", 0)
            existing["claimed"] = queue.get("claimed", 0)
            existing["dead_lettered"] = queue.get("dead_lettered", 0)
            existing["oldest_ready_age_seconds"] = max(
                float(existing.get("oldest_ready_age_seconds", 0) or 0),
                float(queue.get("oldest_ready_age_seconds", 0) or 0),
            )
        return {
            "recorded_at": current["recorded_at"],
            "timestamp": current["timestamp"],
            "queues": sorted(queues.values(), key=lambda queue: queue["queue"]),
        }

    async def _set_state(self, state: JobState) -> None:
        state.updated_at = utcnow()
        await self.state_store.set(self._job_key(state.job.id), state)
        async with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"workers:jobs:{job_id}"

    @staticmethod
    def _same_idempotent_job(existing: JobEnvelope, incoming: JobEnvelope) -> bool:
        return existing.idempotency_payload() == incoming.idempotency_payload()


_runtime: WorkerRuntime | None = None

_BACKEND_METHODS: dict[str, tuple[str, ...]] = {
    "state_store": ("get", "set", "delete", "update", "keys"),
    "event_log": ("append", "read", "read_filtered", "subscribe", "delete"),
    "queue": ("submit", "claim", "ack", "nack", "cancel", "wake", "stats"),
    "dead_letter_store": ("create", "get", "list", "save"),
    "archive": (
        "bulk_insert_events",
        "upsert_state_snapshot",
        "query_events",
        "latest_state_snapshot",
        "historical_state_snapshots",
    ),
}


def load_backend_class(spec: str) -> type:
    """Import a worker backend class from a ``module:ClassName`` string."""
    if ":" not in spec:
        raise ValueError(
            f"Invalid worker backend spec {spec!r}: must be in format 'module:ClassName'"
        )
    module_path, class_name = spec.split(":", 1)
    if not module_path or not class_name:
        raise ValueError(
            f"Invalid worker backend spec {spec!r}: must be in format 'module:ClassName'"
        )
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _instantiate_backend(
    spec: str,
    *,
    kind: str,
    settings: Any | None = None,
    session_maker: Any | None = None,
) -> Any:
    cls = load_backend_class(spec)
    kwargs: dict[str, Any] = {}
    signature = inspect.signature(cls)
    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    if settings is not None and (
        accepts_kwargs or "settings" in signature.parameters
    ):
        kwargs["settings"] = settings
    if session_maker is not None and (
        accepts_kwargs or "session_maker" in signature.parameters
    ):
        kwargs["session_maker"] = session_maker
    backend = cls(**kwargs)
    missing = [
        method
        for method in _BACKEND_METHODS[kind]
        if not callable(getattr(backend, method, None))
    ]
    if missing:
        raise TypeError(
            f"Worker backend {spec!r} does not implement {kind}: "
            f"missing {', '.join(missing)}"
        )
    return backend


def _coerce_backend_config(
    backend_imports: WorkerBackendConfig | Mapping[str, str] | Any | None,
) -> WorkerBackendConfig:
    if backend_imports is None:
        return WorkerBackendConfig()
    if isinstance(backend_imports, WorkerBackendConfig):
        return backend_imports
    if isinstance(backend_imports, Mapping):
        return WorkerBackendConfig(**backend_imports)
    if hasattr(backend_imports, "model_dump"):
        return WorkerBackendConfig(**backend_imports.model_dump())
    return WorkerBackendConfig(
        state_store=backend_imports.state_store,
        event_log=backend_imports.event_log,
        queue=backend_imports.queue,
        dead_letter_store=backend_imports.dead_letter_store,
        archive=getattr(backend_imports, "archive", WorkerBackendConfig().archive),
    )


def get_runtime() -> WorkerRuntime:
    global _runtime
    if _runtime is None:
        _runtime = WorkerRuntime()
    return _runtime


def configure_workers(
    *,
    mode: ExecutionMode = "inline",
    queues: tuple[str, ...] = ("default",),
    concurrency: int = 1,
    poll_interval: float = 0.05,
    visibility_timeout: float = 30.0,
    max_reclaims: int = 3,
    backend_imports: WorkerBackendConfig | Mapping[str, str] | Any | None = None,
    settings: Any | None = None,
    session_maker: Any | None = None,
    state_store: StateStore | None = None,
    event_log: EventLog | None = None,
    queue: Queue | None = None,
    dead_letter_store: DeadLetterStore | None = None,
    archive: Archive | None = None,
) -> WorkerRuntime:
    """Configure the process-local private-beta worker runtime."""

    global _runtime
    backends = _coerce_backend_config(backend_imports)
    _runtime = WorkerRuntime(
        config=WorkerConfig(
            mode=mode,
            queues=queues,
            concurrency=concurrency,
            poll_interval=poll_interval,
            visibility_timeout=visibility_timeout,
            max_reclaims=max_reclaims,
        ),
        state_store=state_store
        or _instantiate_backend(
            backends.state_store,
            kind="state_store",
            settings=settings,
            session_maker=session_maker,
        ),
        event_log=event_log
        or _instantiate_backend(
            backends.event_log,
            kind="event_log",
            settings=settings,
            session_maker=session_maker,
        ),
        queue=queue
        or _instantiate_backend(
            backends.queue,
            kind="queue",
            settings=settings,
            session_maker=session_maker,
        ),
        dead_letter_store=dead_letter_store
        or _instantiate_backend(
            backends.dead_letter_store,
            kind="dead_letter_store",
            settings=settings,
            session_maker=session_maker,
        ),
        archive=archive
        or _instantiate_backend(
            backends.archive,
            kind="archive",
            settings=settings,
            session_maker=session_maker,
        ),
    )
    return _runtime


async def submit(
    job_or_type: BaseModel | str,
    payload: BaseModel | dict[str, Any] | None = None,
    **kwargs: Any,
) -> JobHandle:
    """Submit a worker job to the configured runtime."""

    return await get_runtime().submit(job_or_type, payload, **kwargs)


def get_handle(job_id: str) -> JobHandle:
    """Reconstruct a job handle from a job id in the current local runtime."""

    return get_runtime().handle(job_id)


async def wake(job_id: str, *, resume_at: datetime | None = None) -> bool:
    """Wake a paused local worker job."""

    return await get_runtime().wake(job_id, resume_at=resume_at)


@asynccontextmanager
async def local_executor(
    *,
    mode: ExecutionMode = "in_process",
    queues: tuple[str, ...] = ("default",),
    concurrency: int = 1,
):
    """Temporarily install and start a local worker runtime."""

    global _runtime
    previous = _runtime
    runtime = WorkerRuntime(config=WorkerConfig(mode=mode, queues=queues, concurrency=concurrency))
    _runtime = runtime
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()
        _runtime = previous
