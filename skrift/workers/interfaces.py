"""Backend contracts for Skrift workers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from skrift.workers.models import DeadJobEntry, ClaimedJob, JobEnvelope, QueueStats


class BackendCapabilities(frozenset[str]):
    """Declared backend capability set."""


UpdateFn = Callable[[Any], Any | Awaitable[Any]]


class StateStore(Protocol):
    """Async key/value storage with atomic update semantics."""

    capabilities: BackendCapabilities

    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, *, ttl: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def update(self, key: str, fn: UpdateFn, *, ttl: float | None = None) -> Any: ...
    async def keys(self, prefix: str = "") -> list[str]: ...


class EventLog(Protocol):
    """Append-only ordered event log partitioned by stream."""

    capabilities: BackendCapabilities

    async def append(self, stream: str, event: dict[str, Any]) -> int: ...
    async def read(
        self,
        stream: str,
        *,
        from_position: int = 0,
        limit: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]: ...
    async def read_filtered(
        self,
        stream: str,
        *,
        filters: dict[str, Any],
        from_position: int = 0,
        limit: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]: ...
    async def subscribe(
        self,
        stream: str,
        *,
        from_position: int | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]: ...
    async def delete(self, stream: str) -> None: ...
    async def list_streams(self, prefix: str = "") -> list[str]: ...


class Queue(Protocol):
    """Durable work queue with claim/ack semantics."""

    capabilities: BackendCapabilities

    async def submit(self, job: JobEnvelope, *, job_id: str | None = None) -> JobEnvelope: ...
    async def claim(self, queues: list[str], *, visibility_timeout: float) -> ClaimedJob | None: ...
    async def ack(self, queue: str, job_id: str, token: str) -> None: ...
    async def nack(
        self,
        queue: str,
        job_id: str,
        token: str,
        *,
        retry_at: datetime | None = None,
        dead_letter: bool = False,
    ) -> None: ...
    async def cancel(self, queue: str, job_id: str) -> bool: ...
    async def wake(self, queue: str, job_id: str, *, resume_at: datetime | None = None) -> bool: ...
    async def stats(self, queue: str) -> QueueStats: ...


class DeadLetterStore(Protocol):
    """Operator-facing dead-letter records and state transitions."""

    capabilities: BackendCapabilities

    async def create(self, entry: DeadJobEntry) -> DeadJobEntry: ...
    async def get(self, entry_id: str) -> DeadJobEntry | None: ...
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
    ) -> list[DeadJobEntry]: ...
    async def save(self, entry: DeadJobEntry) -> DeadJobEntry: ...


class Archive(Protocol):
    """Cold storage for events and state snapshots."""

    capabilities: BackendCapabilities

    async def bulk_insert_events(
        self, events: list[tuple[str, int, dict[str, Any]]]
    ) -> None: ...
    async def upsert_state_snapshot(
        self, key: str, value: Any, *, timestamp: datetime | None = None
    ) -> None: ...
    async def query_events(
        self,
        stream: str,
        *,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]: ...
    async def latest_state_snapshot(self, key: str) -> Any: ...
    async def historical_state_snapshots(self, key: str) -> list[tuple[datetime, Any]]: ...
