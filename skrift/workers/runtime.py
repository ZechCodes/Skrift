"""Worker runtime, pools, handles, and public API helpers."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import random
import traceback
import warnings
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

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
DEAD_LETTER_PENDING_PREFIX = "workers:dead_letter_pending:"
QUEUE_WAIT_HISTORY_STATE_KEY = "workers:queue_wait_history"
QUEUE_TREND_HISTORY_STATE_KEY = "workers:queue_trend_history"
# How long a finished job's state stays queryable before the store reclaims it.
# Matches the default of `workers.retention.terminal_job_state_ttl`, which is what
# the pruner uses for the Redis backend; state stores without a pruner (notably the
# in-memory one) rely on this write-time TTL plus the reaper's sweep instead.
TERMINAL_JOB_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTERED,
        JobStatus.CANCELLED,
    }
)
ExecutionMode = Literal["inline", "in_process", "out_of_process"]
logger = logging.getLogger(__name__)


class JobFailed(RuntimeError):
    """Raised when awaiting a failed worker job."""


class PermanentFailure(RuntimeError):
    """Raised by handlers to skip remaining retries and dead-letter immediately."""


class JobCancelled(asyncio.CancelledError):
    """Raised when awaiting a cancelled worker job."""


class _Drained(Exception):
    """A job's handler was stopped by its worker pool's drain."""


class _Abandoned(Exception):
    """A job's handler ended after its worker pool's drain abandoned its run."""


class _StateRefused(Exception):
    """A job state change found a stored state it does not apply to."""


class _NoDeadLetterHandler(Exception):
    """A pending dead letter's job type has no registered handler to call back."""


@dataclass(frozen=True)
class WorkerConfig:
    """Runtime settings for the MVP local worker executor."""

    mode: ExecutionMode = "inline"
    queues: tuple[str, ...] = ("default",)
    concurrency: int = 1
    # How many claimed jobs each worker may run at once.
    max_inflight_per_worker: int = 1
    poll_interval: float = 0.05
    max_poll_interval: float = 2.0
    poll_backoff_factor: float = 2.0
    visibility_timeout: float = 30.0
    reaper_interval: float = 5.0
    max_reclaims: int = 3
    terminal_job_state_ttl: float | None = TERMINAL_JOB_STATE_TTL_SECONDS
    # On stop: how long running jobs get to finish, then how long a cancelled
    # job's handler gets to stop before it is left behind.
    drain_timeout: float = 20.0
    drain_cancel_timeout: float = 5.0
    # Asked before each claim whether a worker takes another job (#207).
    governor: Callable[[WorkerRuntime, int], bool | Awaitable[bool]] | None = None

    def __post_init__(self) -> None:
        # A pool with no places would start and never claim a job.
        for setting in ("concurrency", "max_inflight_per_worker"):
            if getattr(self, setting) < 1:
                raise ValueError(f"{setting} must be at least 1, got {getattr(self, setting)}")


@dataclass(frozen=True)
class WorkerBackendConfig:
    """Import paths for backend implementations."""

    state_store: str = "skrift.workers.memory:InMemoryStateStore"
    event_log: str = "skrift.workers.memory:InMemoryEventLog"
    queue: str = "skrift.workers.memory:InMemoryQueue"
    dead_letter_store: str = "skrift.workers.memory:InMemoryDeadLetterStore"
    archive: str = "skrift.workers.memory:InMemoryArchive"


@dataclass
class _Run:
    """One execution of a claim: its id, its claim's order, and the state it replaced.

    ``order`` is None for inline runs, which no other run can overlap. A run
    a drain abandoned in its handler settles nothing: its claim is left to
    expire, so the process can exit whenever it likes. ``on_abandon`` is
    called then, to stop anything keeping the claim alive.
    """

    run_id: str | None
    order: int | None
    replaced: JobState | None = None
    job_id: str | None = None
    abandoned: bool = False
    on_abandon: list[Callable[[], None]] = field(default_factory=list)


@dataclass
class _Polling:
    """A worker's turn at polling its queues, shared by its places, and the
    wait after an empty poll."""

    interval: float
    turn: asyncio.Lock = field(default_factory=asyncio.Lock)
    # How many of the worker's places hold a claimed job, from its claim to its
    # settlement; and whether the governor's last answer was a failure.
    inflight: int = 0
    governor_failing: bool = False


@dataclass
class WorkerContext:
    """Context passed to handlers that accept a second argument."""

    runtime: "WorkerRuntime"
    job: JobEnvelope
    paused_state: dict[str, Any]
    # Orders this run's claim among the job's claims: a later claim has a
    # greater value. None for inline runs, and if the queue supplies none.
    claim_order: int | None = None

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
    """Runs N concurrent in-process workers.

    Each worker has ``max_inflight_per_worker`` places, each running one claimed
    job at a time; a place claims its own job, so it is the task that holds
    the claim. A worker polls as one: its free places take turns, one claim at
    a time, and share the wait after an empty poll. A ``governor``, if given,
    is asked within the turn before each claim, and a no waits as an empty
    poll does.
    """

    def __init__(
        self,
        runtime: "WorkerRuntime",
        *,
        queues: list[str],
        concurrency: int = 1,
        max_inflight_per_worker: int = 1,
        poll_interval: float = 0.05,
        max_poll_interval: float = 2.0,
        poll_backoff_factor: float = 2.0,
        governor: Callable[[WorkerRuntime, int], bool | Awaitable[bool]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._governor = governor
        self._queues = queues
        self._concurrency = concurrency
        self._max_inflight = max_inflight_per_worker
        self._poll_interval = poll_interval
        self._max_poll_interval = max_poll_interval
        self._poll_backoff_factor = poll_backoff_factor
        self._tasks: list[asyncio.Task] = []
        # Worker tasks running a claimed job, from its claim to its settlement,
        # and those sleeping between polls.
        self._busy: set[asyncio.Task] = set()
        self._idling: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()
        self._drain: asyncio.Task | None = None

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping.clear()
        self._drain = None
        self._tasks = []
        for i in range(self._concurrency):
            polling = _Polling(self._poll_interval)
            self._tasks += [
                asyncio.create_task(
                    self._run_worker(polling), name=f"skrift-worker-{i}" + (f".{j}" if j else "")
                )
                for j in range(self._max_inflight)
            ]

    async def stop(self) -> list[str]:
        """Stop claiming, then drain the jobs already running.

        Every call waits for the same drain.

        They get ``drain_timeout`` to finish. Then any still in their handler
        are cancelled and handed back to the queue, and any worker stuck
        claiming is cancelled. A handler still running ``drain_cancel_timeout``
        later is abandoned with its claim, to expire; a job's own ack, nack
        and state writes are always waited for.

        Returns the ids of the jobs abandoned with their claims held: those
        whose handler ignored its cancellation, still running as tasks but
        fenced off from settling, and those the queue failed to take back.

        A cancelled call still waits for the drain before it raises.
        """
        if self._drain is None:
            self._drain = asyncio.create_task(self._drain_workers(), name="skrift-worker-drain")
        try:
            return await asyncio.shield(self._drain)
        except asyncio.CancelledError:
            # A cancelled caller still waits out the drain, which is bounded,
            # so its jobs settle before the cancellation goes on; unless it is
            # a job's own worker, which the drain is waiting for.
            if asyncio.current_task() in self._busy:
                raise
            while not self._drain.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.wait({self._drain})
            raise

    async def _drain_workers(self) -> list[str]:
        self._stopping.set()
        for task in self._idling:
            task.cancel()
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return []
        config = self._runtime.config
        _, running = await asyncio.wait(tasks, timeout=config.drain_timeout)
        if running:
            self._runtime._stop_handlers(running)
            for task in running - self._busy:
                task.cancel()
            abandoned = await self._runtime._abandon_handlers(
                running, timeout=config.drain_cancel_timeout
            )
            # The rest are settling their jobs, or cancelled while claiming.
            writing = {task for task in running - abandoned if not task.done()}
            if writing:
                await asyncio.wait(writing)
        return list(self._runtime._abandoned)

    async def _run_worker(self, polling: _Polling | None = None) -> None:
        """Run one place of a worker; ``polling`` is shared by its places."""
        polling = polling or _Polling(self._poll_interval)
        while not self._stopping.is_set():
            try:
                claimed = await self._claim(polling)
                if claimed is None:
                    continue
                task = asyncio.current_task()
                self._busy.add(task)
                polling.inflight += 1
                try:
                    await self._runtime.execute_claim(claimed)
                finally:
                    self._busy.discard(task)
                    polling.inflight -= 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Worker loop error; continuing", exc_info=True)
                await self._idle(self._poll_interval)

    async def _idle(self, seconds: float) -> None:
        """Sleep between polls; a stop cancels the sleep."""
        task = asyncio.current_task()
        self._idling.add(task)
        try:
            await asyncio.sleep(seconds)
        finally:
            self._idling.discard(task)

    async def _claim(self, polling: _Polling) -> ClaimedJob | None:
        """Take the worker's turn to poll, and claim a job; None after an empty poll."""
        async with polling.turn:
            if self._stopping.is_set():
                return None
            if self._governor is not None:
                admitted = await self._ask_governor(polling)
                # A stop that began while the governor was deciding admits
                # nothing, whatever it answered.
                if self._stopping.is_set():
                    return None
                if not admitted:
                    await self._back_off(polling)
                    return None
            try:
                claimed = await self._runtime.queue.claim(
                    self._queues, visibility_timeout=self._runtime.default_visibility_timeout
                )
            except Exception:
                logger.warning("Worker loop error; continuing", exc_info=True)
                await self._idle(self._poll_interval)
                return None
            if claimed is None:
                await self._back_off(polling)
                return None
            polling.interval = self._poll_interval
            return claimed

    async def _back_off(self, polling: _Polling) -> None:
        """Wait after a poll that claimed nothing, longer each time up to
        ``max_poll_interval``; a claim resets the wait."""
        await self._idle(polling.interval)
        polling.interval = min(
            polling.interval * self._poll_backoff_factor,
            self._max_poll_interval,
        )

    async def _ask_governor(self, polling: _Polling) -> bool:
        """Whether the governor lets this worker claim another job.

        A governor that raises, or answers anything but a bool, is taken as a
        no; that is logged once until it answers True or False again. While it
        decides, the place counts as idling, so a stop cancels the decision.
        """
        task = asyncio.current_task()
        self._idling.add(task)
        try:
            answer = self._governor(self._runtime, polling.inflight)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception:
            if not polling.governor_failing:
                logger.warning(
                    "Worker governor raised; claiming nothing until it answers",
                    exc_info=True,
                )
            polling.governor_failing = True
            return False
        finally:
            self._idling.discard(task)
        if not isinstance(answer, bool):
            if not polling.governor_failing:
                logger.warning(
                    "Worker governor answered %r, not True or False; claiming nothing "
                    "until it answers",
                    answer,
                )
            polling.governor_failing = True
            return False
        if polling.governor_failing:
            logger.info("Worker governor answered %s again; claiming as it decides", answer)
            polling.governor_failing = False
        return answer


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
        # Counts state writes, so a waiter can tell one landed while it read.
        self._state_writes = 0
        self._pool: WorkerPool | None = None
        # Counts stop calls, so a start can tell one landed while it awaited.
        self._stops = 0
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
        self._reaper_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        # Worker tasks inside a claimed job's handler, with the job's run; those a
        # drain cancelled; set whenever a handler ends; whether a drain is
        # stopping handlers, so none starts; and the jobs a drain abandoned with
        # their claims held.
        self._handler_tasks: dict[asyncio.Task, _Run] = {}
        self._drained_tasks: set[asyncio.Task] = set()
        self._handler_ended = asyncio.Event()
        self._stopping_handlers = False
        self._abandoned: list[str] = []
        self._nack_accepts_job: tuple[Queue, bool] | None = None
        self._queue_history_interval = 2.0
        self._queue_history_idle_interval = 30.0
        self._queue_history_current_interval = self._queue_history_interval
        self._queue_trend_bucket_seconds = self._queue_history_interval

    async def start(self) -> None:
        if self.config.mode != "in_process":
            return
        # A stop that lands while this start awaits wins: the start goes no
        # further, so nothing it would install outlives that stop.
        stops = self._stops
        await self.record_queue_history()
        if self._stops != stops or self._pool is not None:
            return  # stopped, or another start installed the pool
        self._stopping_handlers = False
        self._abandoned = []
        self._pool = WorkerPool(
            self,
            queues=list(self.config.queues),
            concurrency=self.config.concurrency,
            max_inflight_per_worker=self.config.max_inflight_per_worker,
            poll_interval=self.config.poll_interval,
            max_poll_interval=self.config.max_poll_interval,
            poll_backoff_factor=self.config.poll_backoff_factor,
            governor=self.config.governor,
        )
        await self._pool.start()
        if self._stops != stops:
            return  # that stop stopped the pool
        if self._queue_history_task is None:
            self._queue_history_task = asyncio.create_task(
                self._record_queue_history_loop(),
                name="skrift-worker-queue-history",
            )
        if self._reconcile_task is None:
            # In the background, once the pool polls: a slow store cannot hold
            # up start, and a dead callback can wait on queued work.
            self._reconcile_task = asyncio.create_task(
                self._reconcile_dead_letters_on_start(),
                name="skrift-worker-dead-letter-reconcile",
            )
        if self._reaper_task is None and hasattr(self.queue, "_release_expired_claims"):
            self._reaper_task = asyncio.create_task(
                self._release_expired_claims_loop(),
                name="skrift-worker-reaper",
            )

    async def stop(self) -> list[str]:
        """Stop the worker pool, draining it; see ``WorkerPool.stop``.

        Returns the ids of the jobs the drain abandoned with their claims held,
        from every call until the runtime starts again. A handler among them
        that ignored its cancellation is still running: a process should exit
        without waiting for it. A cancelled call still drains the pool, and
        waits for the drain, before it raises.
        """
        self._stops += 1
        try:
            if self._reconcile_task is not None:
                self._reconcile_task.cancel()
                await asyncio.gather(self._reconcile_task, return_exceptions=True)
                self._reconcile_task = None
            if self._reaper_task is not None:
                self._reaper_task.cancel()
                await asyncio.gather(self._reaper_task, return_exceptions=True)
                self._reaper_task = None
            if self._queue_history_task is not None:
                self._queue_history_task.cancel()
                await asyncio.gather(self._queue_history_task, return_exceptions=True)
                self._queue_history_task = None
        finally:
            if self._pool is not None:
                await self._pool.stop()
                self._pool = None
        return list(self._abandoned)

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
            return await self._dead_letter_submission(job, exc)

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
        existing_state = await self._record_new_job_state(
            JobState(job=job, status=JobStatus.SUBMITTED)
        )
        if existing_state is not None:
            if self._same_idempotent_job(existing_state.job, job):
                return JobHandle(self, job.id)
            raise JobIdConflict(f"job id {job.id!r} already exists")
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

    async def submit_inline(
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
        metadata: dict[str, Any] | None = None,
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
                metadata=metadata,
            )
            return await self._dead_letter_submission(job, exc)

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
            metadata=metadata,
        )
        existing_state = await self._record_new_job_state(
            JobState(job=job, status=JobStatus.SUBMITTED)
        )
        if existing_state is not None:
            if self._same_idempotent_job(existing_state.job, job):
                return JobHandle(self, job.id)
            raise JobIdConflict(f"job id {job.id!r} already exists")
        await self.emit_lifecycle(LifecycleEventType.JOB_SUBMITTED, job)
        handle = JobHandle(self, job.id)
        await self.execute_claim(ClaimedJob(job=job, token="inline"), inline=True)
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
        metadata: dict[str, Any] | None = None,
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
            metadata=metadata or {},
        )

    async def get_job_state(self, job_id: str) -> JobState | None:
        return await self.state_store.get(self._job_key(job_id))

    async def wait_for_result(self, job_id: str, *, timeout: float | None = None) -> Any:
        async def _wait() -> Any:
            while True:
                writes = self._state_writes
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
                    while self._state_writes == writes:
                        await self._condition.wait()

        if timeout is None:
            return await _wait()
        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def cancel(self, job_id: str) -> bool:
        state = await self.get_job_state(job_id)
        if state is None or state.status != JobStatus.SUBMITTED:
            return False
        removed = await self.queue.cancel(state.job.queue, job_id)
        if not removed and self.config.mode != "inline":
            return False
        cancelled_state, _ = await self._change_state(
            job_id, lambda current: self._cancelled_state(current, removed=removed)
        )
        if cancelled_state is None:
            return False
        await self.emit_lifecycle(LifecycleEventType.JOB_CANCELLED, state.job)
        return True

    @staticmethod
    def _cancelled_state(current: JobState | None, *, removed: bool) -> JobState | None:
        """The CANCELLED state ``cancel`` writes over ``current``, or None to refuse.

        ``removed`` is whether this cancel's ``queue.cancel`` deleted the job's
        queue entry. If it did, the job never runs again, so any unsettled
        state is cancelled: SUBMITTED, PAUSED by a run that claimed the job
        meanwhile, or RUNNING from a run whose claim expired. Otherwise (an
        inline job, with no entry) only SUBMITTED is: its run has not started.
        A settled job, including a dead-lettered entry the queue removed, is
        left as is. The run id is dropped so a run whose claim expired does not
        put back the state its start replaced.
        """
        if current is None or current.status in TERMINAL_JOB_STATUSES:
            return None
        if not removed and current.status != JobStatus.SUBMITTED:
            return None
        return current.model_copy(update={"status": JobStatus.CANCELLED, "run_id": None})

    async def wake(self, job_id: str, *, resume_at: datetime | None = None) -> bool:
        state = await self.get_job_state(job_id)
        if state is None:
            return False
        if (
            state.status == JobStatus.PAUSED
            and state.job.metadata.get("skrift_dispatch") == "inline"
        ):
            return await self._resume_inline(job_id, state.run_id, resume_at=resume_at)
        if (
            state.status == JobStatus.PAUSED
            and state.job.metadata.get("skrift_dispatch") == "inline_then_queued"
        ):

            def resubmit(current: JobState | None) -> JobState | None:
                if current is None or current.status != JobStatus.PAUSED:
                    return None
                current.job.scheduled_for = resume_at
                return JobState(
                    job=current.job,
                    status=JobStatus.SUBMITTED,
                    attempt=current.attempt,
                    paused_state=current.paused_state,
                    attempt_history=current.attempt_history,
                )

            submitted, _ = await self._change_state(job_id, resubmit)
            if submitted is None:
                return False
            await self.queue.submit(submitted.job, job_id=job_id)
            return True
        return await self.queue.wake(state.job.queue, job_id, resume_at=resume_at)

    async def _resume_inline(
        self, job_id: str, paused_by: str | None, *, resume_at: datetime | None
    ) -> bool:
        """Resume a paused inline job from the pause the run ``paused_by`` wrote.

        Only the wake (or pause timer) that moves the job out of that pause runs
        it; any other finds it gone and returns False, having emitted nothing.
        """

        def resume(current: JobState | None) -> JobState | None:
            if (
                current is None
                or current.status != JobStatus.PAUSED
                or current.run_id != paused_by
            ):
                return None
            return JobState(
                job=current.job.model_copy(update={"scheduled_for": resume_at}),
                status=JobStatus.SUBMITTED,
                attempt=current.attempt,
                paused_state=current.paused_state,
                attempt_history=current.attempt_history,
                run_id=wake_id,
            )

        wake_id = uuid4().hex
        woken, _ = await self._change_state(job_id, resume)
        if woken is None:
            return False
        if resume_at is not None and resume_at > utcnow():
            await asyncio.sleep((resume_at - utcnow()).total_seconds())
        # The run starts only if the job is still the state this wake wrote:
        # a cancel since then drops the wake's id.
        return await self.execute_claim(
            ClaimedJob(job=woken.job, token="inline"), inline=True, wake_id=wake_id
        )

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
            "visibility_timeout": self.config.visibility_timeout,
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
        replayed = JobState(job=job, status=JobStatus.SUBMITTED)
        if await self._record_new_job_state(replayed) is not None:
            raise JobIdConflict(f"job id {job.id!r} already exists")
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
    ) -> bool:
        """Store one compressed oldest-ready wait bucket for all known queues.

        Returns whether any queue was carrying work (ready, delayed, or claimed
        jobs) so callers such as the recording loop can back off while idle.
        """
        timestamp = utcnow()

        if queue_stats is None:
            states, _ = await self._job_states_with_total(limit=100)
            queue_stats = [
                await self.queue.stats(queue)
                for queue in self._queue_names(states)
            ]

        had_activity = any(
            stats.ready or stats.delayed or stats.claimed for stats in queue_stats
        )

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
                    return had_activity
            self._queue_history.append(sample)
            await self._persist_queue_history_locked()
            await self._persist_queue_trend_history_locked()
        return had_activity

    async def execute_claim(
        self, claimed: ClaimedJob, *, inline: bool = False, wake_id: str | None = None
    ) -> bool:
        """Run a claimed job and settle its outcome; returns whether the run started.

        ``wake_id`` is set by the ``wake`` of a paused inline job: the run
        starts only from the SUBMITTED state that wake wrote.
        """
        if inline:
            # An inline claim carries the envelope of the state it was read
            # from, which the in-memory store keeps as is: the run works on its
            # own copy, so a start that is refused leaves the stored state alone.
            claimed = claimed.model_copy(update={"job": claimed.job.model_copy(deep=True)})
        job = claimed.job
        if not inline and self._claim_expired(claimed):
            logger.warning(
                "Job %s reached its worker after its claim expired; skipping the run "
                "(the claim is released for another worker)",
                job.id,
            )
            return False
        if job.reclaim_count >= job.max_reclaims:
            await self._dead_letter_claim(
                claimed,
                cause=DeadLetterCause.RECLAIM_LOOP,
                error=f"Job reclaimed {job.reclaim_count} times",
                inline=inline,
            )
            return False
        descriptor = self.registry.get(job.type)
        job.attempt += 1
        started_at = utcnow()
        previous_state = await self.get_job_state(job.id)
        attempt_history = previous_state.attempt_history if previous_state is not None else []
        run = await self._start_run(
            JobState(
                job=job,
                status=JobStatus.RUNNING,
                attempt=job.attempt,
                paused_state=previous_state.paused_state if previous_state is not None else {},
                attempt_history=attempt_history,
            ),
            order=None if inline else claimed.claim_order,
            inline=inline,
            wake_id=wake_id,
        )
        if run is None:
            if inline:
                logger.info(
                    "Job %s was cancelled or finished before its inline run started; "
                    "skipping the run",
                    job.id,
                )
                return False
            logger.warning(
                "Job %s was claimed again after this worker's claim; skipping the run",
                job.id,
            )
            return False
        await self.emit_lifecycle(LifecycleEventType.JOB_CLAIMED, job)
        if wake_id is not None or (
            previous_state is not None and previous_state.status == JobStatus.PAUSED
        ):
            await self.emit_lifecycle(LifecycleEventType.JOB_RESUMED, job)
        await self.emit_lifecycle(LifecycleEventType.JOB_STARTED, job)
        try:
            result = await self._run_handler(descriptor, job, inline=inline, run=run)
        except _Abandoned:
            logger.warning(
                "Job %s's handler ended after its worker's drain abandoned it; settling "
                "nothing, so its claim expires and another worker takes the job",
                job.id,
            )
            return True
        except _Drained:
            await self._hand_back(claimed, previous_state, run=run)
            return True
        except PermanentFailure as exc:
            await self._handle_failure(
                claimed,
                descriptor.retry_policy,
                exc,
                inline=inline,
                started_at=started_at,
                run=run,
                permanent=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            await self._handle_failure(
                claimed,
                descriptor.retry_policy,
                exc,
                inline=inline,
                started_at=started_at,
                run=run,
            )
            return True

        if isinstance(result, Pause):
            await self._handle_pause(claimed, result, inline=inline, run=run)
            return True

        # The ack checks the claim token atomically with its write, so it
        # decides whether this worker still owns the job.
        if not inline and not await self._settle_claim(
            job, self.queue.ack(job.queue, job.id, claimed.token), run=run
        ):
            return True
        if not await self._set_settled_state(
            JobState(
                job=job,
                status=JobStatus.COMPLETED,
                attempt=job.attempt,
                result=result,
                attempt_history=attempt_history,
            ),
            run,
        ):
            return True
        await self.emit_lifecycle(LifecycleEventType.JOB_COMPLETED, job)
        return True

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

    async def _call_handler(
        self, descriptor: HandlerDescriptor, job: JobEnvelope, run: _Run
    ) -> Any:
        payload = descriptor.payload_model.model_validate(job.payload)
        state = await self.get_job_state(job.id)
        context = WorkerContext(
            runtime=self,
            job=job,
            paused_state=state.paused_state if state is not None else {},
            claim_order=run.order,
        )
        signature = inspect.signature(descriptor.func)
        if len(signature.parameters) >= 2:
            result = descriptor.func(payload, context)
        else:
            result = descriptor.func(payload)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _run_handler(
        self, descriptor: HandlerDescriptor, job: JobEnvelope, *, inline: bool, run: _Run
    ) -> Any:
        """Call a claimed job's handler; raise ``_Drained`` if a drain stopped it,
        or ``_Abandoned`` if a drain abandoned its run.

        A handler a drain cancelled is drained however it ends, unless it
        returns: one that turns its ``CancelledError`` into another exception
        has not failed.
        """
        task = None if inline else asyncio.current_task()
        if task is None:
            return await self._call_handler(descriptor, job, run)
        if self._stopping_handlers:
            raise _Drained
        self._handler_tasks[task] = run
        try:
            result = await self._call_handler(descriptor, job, run)
        except BaseException:
            if run.abandoned:
                raise _Abandoned from None
            if task in self._drained_tasks:
                raise _Drained from None
            raise
        finally:
            self._handler_tasks.pop(task, None)
            self._handler_ended.set()
            if task in self._drained_tasks:
                self._drained_tasks.discard(task)
                task.uncancel()
        # A drain abandons only runs still in their handler, so a run that gets
        # past this check, with no await since leaving its handler, is never
        # abandoned: it settles in full.
        if run.abandoned:
            raise _Abandoned
        return result

    def _in_handler(self, task: asyncio.Task) -> bool:
        return task in self._handler_tasks

    def _on_abandon(self, callback: Callable[[], None]) -> None:
        """Have a drain call ``callback`` if it abandons the run of the handler
        the current task is in."""
        run = self._handler_tasks.get(asyncio.current_task())
        if run is not None:
            run.on_abandon.append(callback)

    async def _abandon_handlers(
        self, tasks: set[asyncio.Task], *, timeout: float
    ) -> set[asyncio.Task]:
        """Give these worker tasks ``timeout`` to end their handlers, then abandon
        the runs of those still in one; returns those.

        Each task is checked and, if still in its handler, fenced with no await
        in between: its run either settles in full, or, abandoned, settles
        nothing however its handler ends.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._handler_ended.clear()
            inside = {task for task in tasks if self._in_handler(task)}
            remaining = deadline - loop.time()
            if not inside or remaining <= 0:
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(self._handler_ended.wait(), remaining)
        for task in inside:
            run = self._handler_tasks[task]
            run.abandoned = True
            for callback in run.on_abandon:
                callback()
            self._abandoned.append(run.job_id)
            logger.warning(
                "Job %s ignored its cancellation for %ss; abandoning it running on "
                "worker %s, with its claim held until the claim expires",
                run.job_id,
                timeout,
                task.get_name(),
            )
        return inside

    def _stop_handlers(self, tasks: set[asyncio.Task]) -> None:
        """Cancel the handlers these worker tasks are running, and start no more."""
        self._stopping_handlers = True
        for task in tasks & self._handler_tasks.keys():
            self._drained_tasks.add(task)
            task.cancel()

    async def _hand_back(
        self, claimed: ClaimedJob, previous_state: JobState | None, *, run: _Run
    ) -> None:
        """Release a claim whose run a drain stopped, for another worker to take now.

        The run charges no attempt, and a nack is not a reclaim. The job's
        state goes back to submitted, keeping the pause state it resumed from.
        If the queue fails to take the job back, the run's own state is put
        back and the job is abandoned with its claim, to expire.
        """
        job = claimed.job.model_copy(update={"attempt": max(0, claimed.job.attempt - 1)})
        handed_back = JobState(
            job=job,
            status=JobStatus.SUBMITTED,
            attempt=job.attempt,
            last_error=previous_state.last_error if previous_state is not None else None,
            paused_state=previous_state.paused_state if previous_state is not None else {},
            attempt_history=previous_state.attempt_history if previous_state is not None else [],
        )
        handed_back.run_id, handed_back.run_order = run.run_id, run.order
        written, running = await self._write_state_if(
            handed_back, lambda current: self._not_superseded(current, run)
        )
        if not written:
            self._log_claim_lost(job)
            return
        try:
            settled = await self._settle_claim(
                job, self._nack(job, claimed.token, retry_at=utcnow()), run=run
            )
        except Exception as exc:  # noqa: BLE001 - any failure leaves the claim held
            if running is not None:
                await self._write_state_if(
                    running.model_copy(),
                    lambda current: current is not None and current.run_id == run.run_id,
                )
            self._abandoned.append(job.id)
            logger.error(
                "Job %s could not be handed back to the queue (%r); abandoning it "
                "with its claim held until the claim expires",
                job.id,
                exc,
            )
            return
        if not settled:
            return
        logger.info(
            "Job %s was still running when its worker stopped; released it for another worker",
            job.id,
        )

    async def _handle_failure(
        self,
        claimed: ClaimedJob,
        retry_policy: RetryPolicy,
        exc: Exception,
        *,
        inline: bool,
        started_at: datetime,
        run: _Run,
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
        if permanent or job.attempt >= job.max_attempts:
            if not inline and not await self._settle_claim(
                job, self._nack(job, claimed.token, dead_letter=True), run=run
            ):
                return
            await self.emit_lifecycle(LifecycleEventType.JOB_FAILED, job, error=error)
            await self._dead_letter(
                job,
                cause=(
                    DeadLetterCause.PERMANENT_FAILURE
                    if permanent
                    else DeadLetterCause.RETRIES_EXHAUSTED
                ),
                attempts=attempts,
                error=error,
                run=run,
            )
            return

        retry_at = utcnow() + timedelta(seconds=self._retry_delay(retry_policy, job.attempt))
        # Recorded before the nack so the next run inherits this attempt; the
        # write is dropped if another run already owns the job's state.
        if not await self._set_run_state(
            JobState(
                job=job,
                status=JobStatus.SUBMITTED,
                attempt=job.attempt,
                last_error=error,
                attempt_history=attempts,
            ),
            run,
        ):
            self._log_claim_lost(job)
            return
        if inline:
            await self.emit_lifecycle(LifecycleEventType.JOB_FAILED, job, error=error)
            await self.execute_claim(ClaimedJob(job=job, token="inline"), inline=True)
            return
        if not await self._settle_claim(
            job, self._nack(job, claimed.token, retry_at=retry_at), run=run
        ):
            return
        await self.emit_lifecycle(LifecycleEventType.JOB_FAILED, job, error=error)

    async def _handle_pause(
        self, claimed: ClaimedJob, pause: Pause, *, inline: bool, run: _Run
    ) -> None:
        job = claimed.job.model_copy(
            update={"attempt": max(0, claimed.job.attempt - 1), "scheduled_for": pause.resume_at}
        )
        previous_state = await self.get_job_state(job.id)
        # Recorded before the nack so a run resumed right after it sees this
        # pause state; dropped if another run already owns the job's state.
        if not await self._set_run_state(
            JobState(
                job=job,
                status=JobStatus.PAUSED,
                attempt=job.attempt,
                paused_state=pause.state,
                attempt_history=(
                    previous_state.attempt_history if previous_state is not None else []
                ),
            ),
            run,
        ):
            self._log_claim_lost(job)
            return
        if inline:
            await self.emit_lifecycle(LifecycleEventType.JOB_PAUSED, job)
            if pause.resume_at is None:
                return
            delay = max(0.0, (pause.resume_at - utcnow()).total_seconds())
            if delay:
                await asyncio.sleep(delay)
            # Resumed as a wake would: not if a wake already resumed this pause.
            await self._resume_inline(job.id, run.run_id, resume_at=pause.resume_at)
            return
        retry_at = pause.resume_at or datetime.max.replace(tzinfo=utcnow().tzinfo)
        if not await self._settle_claim(
            job, self._nack(job, claimed.token, retry_at=retry_at), run=run
        ):
            return
        await self.emit_lifecycle(LifecycleEventType.JOB_PAUSED, job)

    async def _settle_claim(
        self,
        job: JobEnvelope,
        settle: Awaitable[None],
        *,
        run: _Run | None = None,
    ) -> bool:
        """Run the ack or nack that ends this worker's claim; False if it was lost.

        The queue checks the claim token atomically with its write, so it
        decides ownership. A worker whose claim expired and was taken over
        records nothing else for the job: no lifecycle events, no dead letter.
        If ``run`` is given, the job state is put back to what it was before the
        run started, while the stored state is still the run's own: the next run
        then starts from neither this run's RUNNING nor its error or pause state,
        and a run that started after another run finished leaves that result.
        """
        try:
            await settle
        except ValueError:
            self._log_claim_lost(job)
            if run is not None and run.replaced is not None:
                await self._write_state_if(
                    run.replaced.model_copy(),
                    lambda current: current is not None and current.run_id == run.run_id,
                )
            return False
        return True

    def _claim_expired(self, claimed: ClaimedJob) -> bool:
        """Whether the queue's lease on this claim has certainly run out.

        ``claimed_at`` is taken on this host after the queue started the lease,
        so the estimate errs late and a claim the queue still holds is never
        skipped. Queues that do not report the lease are never judged expired.
        """
        if claimed.visibility_timeout is None:
            return False
        expires_at = claimed.claimed_at + timedelta(seconds=claimed.visibility_timeout)
        return utcnow() >= expires_at

    async def _start_run(
        self,
        running: JobState,
        *,
        order: int | None,
        inline: bool = False,
        wake_id: str | None = None,
    ) -> _Run | None:
        """Write a run's RUNNING state unless a later claim's run has written.

        Returns None, writing nothing, if it has: this worker's claim expired and
        the job was claimed again. What the write replaced is kept so a run whose
        claim turns out to be lost can put it back. An inline run has no claim
        for ``cancel`` to remove, so it also returns None once the job is
        cancelled or finished, and a wake's run (``wake_id``) once the job is no
        longer the SUBMITTED state that wake wrote.
        """
        run = _Run(run_id=uuid4().hex, order=order, job_id=running.job.id)
        running.run_id, running.run_order = run.run_id, order
        written, run.replaced = await self._write_state_if(
            running,
            lambda current: (
                not (inline and current is not None and current.status in TERMINAL_JOB_STATUSES)
                and self._not_superseded(current, run)
                and (
                    wake_id is None
                    or (
                        current is not None
                        and current.status == JobStatus.SUBMITTED
                        and current.run_id == wake_id
                    )
                )
            ),
        )
        return run if written else None

    async def _set_settled_state(self, state: JobState, run: _Run | None) -> bool:
        """Write a job's outcome after the queue accepted its ack or dead-letter nack.

        The queue's token check made this run the job's owner, so the write wins
        whatever is stored but a cancel's: a dead-letter nack leaves an unclaimed
        entry that ``cancel`` can delete, and once it has returned True the job
        stays CANCELLED (#224). Returns False, writing nothing, in that case.
        """
        if run is not None:
            state.run_id, state.run_order = run.run_id, run.order
        written, _ = await self._write_state_if(
            state, lambda current: current is None or current.status != JobStatus.CANCELLED
        )
        return written

    @staticmethod
    def _log_claim_lost(job: JobEnvelope) -> None:
        logger.warning(
            "Job %s lost its claim before its outcome was recorded (the claim "
            "expired and was released); dropping this run's outcome",
            job.id,
        )

    async def _set_run_state(self, state: JobState, run: _Run) -> bool:
        """Write a state a run records before its claim is settled (a retry, a pause).

        Returns False, writing nothing, once a later claim's run has written the
        job's state: this run's claim was lost.
        """
        state.run_id, state.run_order = run.run_id, run.order
        written, _ = await self._write_state_if(
            state, lambda current: self._not_superseded(current, run)
        )
        return written

    @staticmethod
    def _not_superseded(current: JobState | None, run: _Run) -> bool:
        """Whether the stored state was written by this run or an earlier claim's.

        A run without an order (inline, or from a queue that supplies none) is
        not ordered against others.
        """
        return (
            run.order is None
            or current is None
            or current.run_order is None
            or (run.run_id is not None and current.run_id == run.run_id)
            or current.run_order < run.order
        )

    async def _write_state_if(
        self, state: JobState, allowed: Callable[[JobState | None], bool]
    ) -> tuple[bool, JobState | None]:
        """Write ``state`` in one state-store update if ``allowed`` accepts the stored one.

        Returns whether it was written and the state it found.
        """
        written, found = await self._change_state(
            state.job.id, lambda current: state if allowed(current) else None
        )
        return written is not None, found

    async def _record_new_job_state(self, state: JobState) -> JobState | None:
        """Write a submitted job's first state if its id has none (an expired
        one counts as none), in one state-store update.

        Returns the state found instead, having written nothing, or None.
        """
        _, found = await self._change_state(
            state.job.id, lambda current: state if current is None else None
        )
        return found

    async def _change_state(
        self, job_id: str, change: Callable[[JobState | None], JobState | None]
    ) -> tuple[JobState | None, JobState | None]:
        """Replace a job's state with ``change`` of the stored one, in one
        state-store update, so no other write lands between the read and the
        write (#217). ``change`` returns None to leave the state as it is.

        Returns the state written (None if ``change`` refused) and the state found.
        """
        found: list[JobState | None] = [None]

        def write(current: JobState | None) -> JobState:
            found[0] = current
            state = change(current)
            if state is None:
                raise _StateRefused
            state.updated_at = utcnow()
            return state

        try:
            written = await self.state_store.update(
                self._job_key(job_id), write, ttl=self._job_state_ttl
            )
        except _StateRefused:
            return None, found[0]
        async with self._condition:
            self._state_writes += 1
            self._condition.notify_all()
        return written, found[0]

    async def _nack(self, job: JobEnvelope, token: str, **kwargs: Any) -> None:
        if self._queue_nack_accepts_job():
            await self.queue.nack(job.queue, job.id, token, job=job, **kwargs)
        else:
            await self.queue.nack(job.queue, job.id, token, **kwargs)

    def _queue_nack_accepts_job(self) -> bool:
        queue = self.queue
        if self._nack_accepts_job is not None and self._nack_accepts_job[0] is queue:
            return self._nack_accepts_job[1]
        accepts = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or (
                parameter.name == "job"
                and parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            )
            for parameter in inspect.signature(queue.nack).parameters.values()
        )
        if not accepts:
            warnings.warn(
                f"{type(queue).__qualname__}.nack() does not accept job=. Custom queues "
                "should accept the claimed envelope as job= and store it; without it the "
                "queue cannot persist a job's attempt, so max_attempts may never "
                "dead-letter a failing job.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._nack_accepts_job = (queue, accepts)
        return accepts

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
        previous_state = await self.get_job_state(job.id)
        if not inline and not await self._settle_claim(
            job, self._nack(job, claimed.token, dead_letter=True)
        ):
            return
        await self._dead_letter(
            job,
            cause=cause,
            attempts=previous_state.attempt_history if previous_state is not None else [],
            error=error,
            # Stamped with this claim's order so no earlier claim's run writes over it.
            run=_Run(run_id=None, order=None if inline else claimed.claim_order),
        )

    async def _dead_letter(
        self,
        job: JobEnvelope,
        *,
        cause: DeadLetterCause,
        attempts: list[DeadJobAttempt],
        error: str,
        run: _Run | None = None,
        state_recorded: bool = False,
    ) -> DeadJobEntry | None:
        entry = DeadJobEntry(
            job=job.model_copy(deep=True),
            queue=job.queue,
            job_type=job.type,
            cause=cause,
            attempts=attempts,
            latest_error=error,
            retention_until=utcnow() + timedelta(days=30),
        )
        # The state goes first, so a cancel either lands before it and the job
        # gets no dead letter, or finds it settled and refuses.
        if not state_recorded and not await self._set_settled_state(
            self._dead_lettered_state(job, attempts, error), run
        ):
            logger.info("Job %s was cancelled before it was dead-lettered", job.id)
            return None
        # The pending marker holds the record until it is saved and announced,
        # so reconcile_dead_letters can finish a dead letter this one could not.
        pending_key = self._dead_letter_pending_key(entry)
        try:
            await self.state_store.set(pending_key, entry)
        except Exception:
            pending_key = None
            logger.warning(
                "Job %s: saving its pending dead-letter marker failed, so its "
                "dead letter cannot be reconciled if its record fails to save",
                job.id,
                exc_info=True,
            )
        try:
            entry = await self._save_dead_letter_record(entry)
        except Exception:
            # Nothing will run the job again, and without a record nothing can
            # replay it until reconciling recreates the record from the marker.
            if pending_key is not None:
                outcome = (
                    "its pending marker keeps the record, and `skrift workers dlq "
                    "reconcile`, or the next worker start, recreates it."
                )
            else:
                outcome = (
                    "it cannot be replayed. Its job state keeps the envelope, "
                    "attempts and error."
                )
            logger.exception(
                "Job %s (queue %s, type %s, cause %s) is DEAD_LETTERED with no "
                "dead-letter record: saving the record failed, so %s",
                job.id,
                job.queue,
                job.type,
                cause.value,
                outcome,
                extra={
                    "job_id": job.id,
                    "queue": job.queue,
                    "job_type": job.type,
                    "cause": cause.value,
                },
            )
            raise
        await self.emit_lifecycle(LifecycleEventType.JOB_DEAD_LETTERED, job, error=error)
        descriptor = self.registry.get(job.type)
        if descriptor.dead_callback is not None:
            await self._call_dead_callback(descriptor, entry)
        if pending_key is not None:
            await self.state_store.delete(pending_key)
        return entry

    async def reconcile_dead_letters(self) -> dict[str, list[Any]]:
        """Finish the dead letters whose pending marker is still stored.

        A marker stays when saving a job's record, emitting its
        ``job_dead_lettered`` event or calling its dead callback failed, or the
        process stopped first. Each marker's record is created unless its id
        is already stored, the event and callback are delivered, and the
        marker is deleted. Two concurrent runs create one record, but either
        may deliver the event and callback: they are at-least-once. A job
        whose handler is not registered gets its record, but keeps its marker
        until the handler is there to call back.

        Returns the job ids ``recovered``, and those that ``failed`` as
        ``{"job_id": ..., "error": ...}``; they keep their markers for the
        next run.
        """
        recovered: list[str] = []
        failed: list[dict[str, str]] = []
        for key in await self.state_store.keys(DEAD_LETTER_PENDING_PREFIX):
            entry = await self.state_store.get(key)
            if not isinstance(entry, DeadJobEntry):
                continue
            try:
                await self._recreate_dead_letter(entry)
                await self.state_store.delete(key)
            except _NoDeadLetterHandler as exc:
                logger.warning(
                    "Cannot finish the dead letter of job %s: %s; its pending "
                    "marker is kept until the handler is registered",
                    entry.job.id,
                    exc,
                    extra={"job_id": entry.job.id},
                )
                failed.append({"job_id": entry.job.id, "error": str(exc)})
                continue
            except Exception as exc:
                logger.exception(
                    "Reconciling the dead letter of job %s failed; its pending "
                    "marker is kept for the next run",
                    entry.job.id,
                    extra={"job_id": entry.job.id},
                )
                failed.append({"job_id": entry.job.id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            logger.info(
                "Reconciled the dead letter of job %s (entry %s)",
                entry.job.id,
                entry.id,
                extra={"job_id": entry.job.id},
            )
            recovered.append(entry.job.id)
        return {"recovered": recovered, "failed": failed}

    async def _save_dead_letter_record(self, entry: DeadJobEntry) -> DeadJobEntry:
        """Create ``entry``'s record. One already stored under its id counts as
        created: a concurrent reconcile, or the live dead letter, saved it first."""
        try:
            return await self.dead_letter_store.create(entry)
        except Exception:
            try:
                existing = await self.dead_letter_store.get(entry.id)
            except Exception:  # noqa: BLE001 - the create's error is the one to raise
                existing = None
            if existing is None:
                raise
            return existing

    async def _recreate_dead_letter(self, entry: DeadJobEntry) -> None:
        if await self.dead_letter_store.get(entry.id) is None:
            entry = await self._save_dead_letter_record(entry)
        try:
            descriptor = self.registry.get(entry.job_type)
        except KeyError:
            raise _NoDeadLetterHandler(
                f"no handler is registered for job type {entry.job_type!r}"
            ) from None
        await self.emit_lifecycle(
            LifecycleEventType.JOB_DEAD_LETTERED, entry.job, error=entry.latest_error
        )
        if descriptor.dead_callback is not None:
            await self._call_dead_callback(descriptor, entry)

    async def _reconcile_dead_letters_on_start(self) -> None:
        try:
            await self.reconcile_dead_letters()
        except Exception:
            logger.exception("Reconciling dead letters on worker start failed")

    async def _dead_letter_submission(
        self, job: JobEnvelope, exc: ValidationError
    ) -> JobHandle:
        """Dead-letter a submission whose payload failed validation.

        Its id must be new, as for any submission (#217): an equal submission
        gets the existing job's handle, and a different one ``JobIdConflict``.
        """
        error = f"{type(exc).__name__}: {exc}"
        attempts = [self._attempt_from_exception(job, exc, started_at=utcnow())]
        existing_state = await self._record_new_job_state(
            self._dead_lettered_state(job, attempts, error)
        )
        if existing_state is not None:
            if self._same_idempotent_job(existing_state.job, job):
                return JobHandle(self, job.id)
            raise JobIdConflict(f"job id {job.id!r} already exists")
        await self._dead_letter(
            job,
            cause=DeadLetterCause.POISON,
            attempts=attempts,
            error=error,
            state_recorded=True,
        )
        return JobHandle(self, job.id)

    @staticmethod
    def _dead_lettered_state(
        job: JobEnvelope, attempts: list[DeadJobAttempt], error: str
    ) -> JobState:
        return JobState(
            job=job,
            status=JobStatus.DEAD_LETTERED,
            attempt=job.attempt,
            error=error,
            last_error=error,
            attempt_history=attempts,
        )

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
            await asyncio.sleep(self._queue_history_current_interval)
            had_activity = await self.record_queue_history()
            self._queue_history_current_interval = self._next_queue_history_interval(
                had_activity=had_activity,
                current_interval=self._queue_history_current_interval,
            )

    def _next_queue_history_interval(
        self, *, had_activity: bool, current_interval: float
    ) -> float:
        """Poll fast while work is flowing; back off geometrically while idle."""
        if had_activity:
            return self._queue_history_interval
        return min(current_interval * 2, self._queue_history_idle_interval)

    async def _release_expired_claims_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.reaper_interval)
            try:
                await self.queue._release_expired_claims(utcnow())
                await self._sweep_expired_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Reaper loop error; continuing", exc_info=True)

    async def _sweep_expired_state(self) -> None:
        """Prune expired state rows on the reaper timer instead of on every read."""
        sweep_expired = getattr(self.state_store, "sweep_expired", None)
        if callable(sweep_expired):
            await sweep_expired()

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

    def _job_state_ttl(self, state: JobState) -> float | None:
        """Expire finished job state after the retention window; keep live jobs."""
        if state.status not in TERMINAL_JOB_STATUSES:
            return None
        return self.config.terminal_job_state_ttl

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"workers:jobs:{job_id}"

    @staticmethod
    def _dead_letter_pending_key(entry: DeadJobEntry) -> str:
        # Keyed by entry too: a later job reusing the id has its own marker.
        return f"{DEAD_LETTER_PENDING_PREFIX}{entry.job.id}:{entry.id}"

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


def load_governor(spec: str) -> Callable[..., Any]:
    """Import a worker governor from a ``module:attribute`` string."""
    module_path, _, name = spec.partition(":")
    if not module_path or not name:
        raise ValueError(
            f"Invalid worker governor {spec!r}: must be in format 'module:attribute'"
        )
    governor = getattr(importlib.import_module(module_path), name)
    if not callable(governor):
        raise TypeError(f"Worker governor {spec!r} is not callable")
    return governor


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
    if _runtime is None:
        raise RuntimeError(
            "Worker runtime not configured. Call configure_workers() or use "
            "local_executor() before submitting worker jobs or starting agent runs."
        )
    return _runtime


def configure_workers(
    *,
    mode: ExecutionMode = "inline",
    queues: tuple[str, ...] = ("default",),
    concurrency: int = 1,
    max_inflight_per_worker: int = 1,
    poll_interval: float = 0.05,
    max_poll_interval: float = 2.0,
    poll_backoff_factor: float = 2.0,
    visibility_timeout: float = 30.0,
    reaper_interval: float = 5.0,
    max_reclaims: int = 3,
    terminal_job_state_ttl: float | None = TERMINAL_JOB_STATE_TTL_SECONDS,
    drain_timeout: float = 20.0,
    drain_cancel_timeout: float = 5.0,
    governor: str | Callable[..., Any] | None = None,
    backend_imports: WorkerBackendConfig | Mapping[str, str] | Any | None = None,
    settings: Any | None = None,
    session_maker: Any | None = None,
    state_store: StateStore | None = None,
    event_log: EventLog | None = None,
    queue: Queue | None = None,
    dead_letter_store: DeadLetterStore | None = None,
    archive: Archive | None = None,
) -> WorkerRuntime:
    """Configure the process-local private-beta worker runtime.

    ``governor`` is a callable, or its ``module:attribute`` import path, which
    is imported here so a bad one fails at startup.
    """

    global _runtime
    backends = _coerce_backend_config(backend_imports)
    if isinstance(governor, str):
        governor = load_governor(governor)
    _runtime = WorkerRuntime(
        config=WorkerConfig(
            mode=mode,
            queues=queues,
            concurrency=concurrency,
            max_inflight_per_worker=max_inflight_per_worker,
            poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
            poll_backoff_factor=poll_backoff_factor,
            visibility_timeout=visibility_timeout,
            reaper_interval=reaper_interval,
            max_reclaims=max_reclaims,
            terminal_job_state_ttl=terminal_job_state_ttl,
            drain_timeout=drain_timeout,
            drain_cancel_timeout=drain_cancel_timeout,
            governor=governor,
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
