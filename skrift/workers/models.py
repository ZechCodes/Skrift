"""Data models for Skrift's private-beta worker subsystem."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """Base class for typed worker payloads."""


class JobStatus(StrEnum):
    SUBMITTED = "submitted"
    CLAIMED = "claimed"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class DeadLetterCause(StrEnum):
    RETRIES_EXHAUSTED = "retries_exhausted"
    PERMANENT_FAILURE = "permanent_failure"
    RECLAIM_LOOP = "reclaim_loop"
    POISON = "poison"


class DeadLetterState(StrEnum):
    OPEN = "open"
    REPLAYED = "replayed"
    DISCARDED = "discarded"


class EventIdConflict(ValueError):
    """Raised when an event_id is reused with a different event payload."""


class JobIdConflict(ValueError):
    """Raised when a job id is reused with a different job envelope."""


class LifecycleEventType(StrEnum):
    JOB_SUBMITTED = "job_submitted"
    JOB_CLAIMED = "job_claimed"
    JOB_STARTED = "job_started"
    JOB_PAUSED = "job_paused"
    JOB_RESUMED = "job_resumed"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_DEAD_LETTERED = "job_dead_lettered"
    JOB_CANCELLED = "job_cancelled"


class RetryPolicy(BaseModel):
    """Retry settings for a submitted job."""

    max_attempts: int = 3
    backoff_seconds: float = 0.0
    jitter_seconds: float = 0.0


class Pause(BaseModel):
    """Handler result that cooperatively re-enqueues a job."""

    resume_at: datetime | None = None
    state: dict[str, Any] = Field(default_factory=dict)


class JobEnvelope(BaseModel):
    """Serialized job metadata stored in queues and state."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    type: str
    queue: str = "default"
    payload: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime = Field(default_factory=utcnow)
    attempt: int = 0
    max_attempts: int = 3
    visibility_timeout: float = 30.0
    scheduled_for: datetime | None = None
    ready_since: datetime | None = None
    correlation_id: str | None = None
    parent_job_id: str | None = None
    replayed_from: str | None = None
    reclaim_count: int = 0
    max_reclaims: int = 3

    def idempotency_payload(self) -> dict[str, Any]:
        """Return stable fields used to compare caller-supplied job id submissions."""

        payload = self.model_dump(mode="json")
        for key in ("attempt", "ready_since", "submitted_at", "reclaim_count"):
            payload.pop(key, None)
        return payload


class ClaimedJob(BaseModel):
    """A queue claim for a job."""

    job: JobEnvelope
    token: str
    claimed_at: datetime = Field(default_factory=utcnow)


class JobState(BaseModel):
    """Queryable state for a job handle."""

    job: JobEnvelope
    status: JobStatus = JobStatus.SUBMITTED
    attempt: int = 0
    result: Any = None
    error: str | None = None
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    paused_state: dict[str, Any] = Field(default_factory=dict)
    attempt_history: list["DeadJobAttempt"] = Field(default_factory=list)


class WorkerLifecycleEvent(BaseModel):
    """Lifecycle event emitted by the worker subsystem."""

    type: LifecycleEventType
    job_id: str
    queue: str
    job_type: str
    attempt: int
    timestamp: datetime = Field(default_factory=utcnow)
    error: str | None = None


class QueueStats(BaseModel):
    """Inspectable queue state for operators and tests."""

    queue: str
    ready: int = 0
    delayed: int = 0
    claimed: int = 0
    dead_lettered: int = 0
    oldest_ready_age_seconds: float = 0.0


class DeadJobAttempt(BaseModel):
    """One failed execution attempt retained for DLQ forensics."""

    attempt: int
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime = Field(default_factory=utcnow)
    worker_id: str | None = None
    duration_seconds: float | None = None
    exception_type: str = ""
    error: str = ""
    traceback: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeadJobEntry(BaseModel):
    """Durable forensic record for a terminal worker failure."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    job: JobEnvelope
    queue: str
    job_type: str
    cause: DeadLetterCause
    state: DeadLetterState = DeadLetterState.OPEN
    attempts: list[DeadJobAttempt] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    retention_until: datetime | None = None
    latest_error: str = ""
    replayed_to_job_id: str | None = None
    discarded_reason: str | None = None
    discarded_at: datetime | None = None
    replayed_at: datetime | None = None
