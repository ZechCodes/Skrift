# Workers Reference

This page documents the worker configuration, built-in backends, custom backend contracts, and worker CLI commands.

For practical examples, see [Workers](../guides/workers.md).

## Configuration

Workers are configured under the `workers:` key in `app.yaml`.

```yaml
workers:
  enabled: true
  preset: distributed
  queues:
    - default
    - slow
  concurrency: 4
  poll_interval: 0.1
  visibility_timeout: 30.0
  max_reclaims: 3
  imports:
    - myapp.jobs
  persistence:
    streams:
      - workers:lifecycle
    stream_prefixes: []
    batch_size: 100
    flush_interval: 1.0
    snapshot_keys:
      - workers:queue_wait_history
    snapshot_prefixes: []
    snapshot_interval: 60.0
  retention:
    enabled: true
    prune_interval: 300.0
    terminal_job_state_ttl: 604800
    redis_event_ttl: 86400
    redis_event_max_entries: 100000
    dead_queue_marker_ttl: 86400
    archive_event_ttl: 7776000
    archive_snapshot_ttl: 2592000
    dlq_resolved_ttl: 2592000
```

### Runtime Options

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `false` | Configure the worker runtime during app startup |
| `preset` | `custom` | Backend and execution preset: `custom`, `local`, `single_node`, `distributed` |
| `execution` | `inline` | Execution mode: `inline`, `in_process`, `out_of_process` |
| `queues` | `["default"]` | Queues served by the in-process runtime and used by operator views |
| `concurrency` | `1` | Number of in-process worker tasks or default standalone worker concurrency |
| `poll_interval` | `0.05` | Seconds a worker waits after an empty claim |
| `visibility_timeout` | `30.0` | Seconds before an unacked claim can be reclaimed |
| `max_reclaims` | `3` | Number of claim timeouts allowed before dead-lettering as a reclaim loop |
| `imports` | `[]` | Modules imported by standalone worker processes and app startup to register handlers |

### Execution Modes

| Mode | Behavior | Typical use |
|------|----------|-------------|
| `inline` | Execute immediately in the submitting coroutine | Tests and simple local development |
| `in_process` | Submit to a queue drained by background tasks in the web process | Single-node deployments |
| `out_of_process` | Web only submits; separate `skrift workers run` processes drain queues | Multi-process and distributed deployments |

`out_of_process` requires shared state, event, queue, and DLQ backends so web and worker processes see the same jobs. The persister additionally requires a shared archive backend. Memory backends are rejected unless an operator passes `--allow-memory-backends` to a local CLI command.

## Presets

| Preset | Sets `execution` | State store | Event log | Queue | DLQ | Archive |
|--------|------------------|-------------|-----------|-------|-----|---------|
| `local` | `inline` | `InMemoryStateStore` | `InMemoryEventLog` | `InMemoryQueue` | `InMemoryDeadLetterStore` | `InMemoryArchive` |
| `single_node` | `in_process` | `SQLAlchemyStateStore` | `SQLAlchemyEventLog` | `SQLAlchemyQueue` | `SQLAlchemyDeadLetterStore` | `SQLAlchemyArchive` |
| `distributed` | `out_of_process` | `RedisStateStore` | `RedisEventLog` | `RedisQueue` | `SQLAlchemyDeadLetterStore` | `SQLAlchemyArchive` |
| `custom` | unchanged | Explicit config or memory default | Explicit config or memory default | Explicit config or memory default | Explicit config or memory default | Explicit config or memory default |

Preset values can be overridden field by field:

```yaml
workers:
  preset: distributed
  backends:
    queue: skrift.workers.sqlalchemy:SQLAlchemyQueue
```

## Backend Configuration

Each backend value is a `module:ClassName` import string.

```yaml
workers:
  preset: custom
  execution: out_of_process
  backends:
    state_store: skrift.workers.redis:RedisStateStore
    event_log: skrift.workers.redis:RedisEventLog
    queue: skrift.workers.redis:RedisQueue
    dead_letter_store: skrift.workers.sqlalchemy:SQLAlchemyDeadLetterStore
    archive: skrift.workers.sqlalchemy:SQLAlchemyArchive
```

Built-in backend import paths:

| Backend type | Memory | SQLAlchemy | Redis |
|--------------|--------|------------|-------|
| State store | `skrift.workers.memory:InMemoryStateStore` | `skrift.workers.sqlalchemy:SQLAlchemyStateStore` | `skrift.workers.redis:RedisStateStore` |
| Event log | `skrift.workers.memory:InMemoryEventLog` | `skrift.workers.sqlalchemy:SQLAlchemyEventLog` | `skrift.workers.redis:RedisEventLog` |
| Queue | `skrift.workers.memory:InMemoryQueue` | `skrift.workers.sqlalchemy:SQLAlchemyQueue` | `skrift.workers.redis:RedisQueue` |
| Dead-letter store | `skrift.workers.memory:InMemoryDeadLetterStore` | `skrift.workers.sqlalchemy:SQLAlchemyDeadLetterStore` | none |
| Archive | `skrift.workers.memory:InMemoryArchive` | `skrift.workers.sqlalchemy:SQLAlchemyArchive` | none |

Redis backends read `redis.url` and `redis.prefix` from settings unless a custom client is injected by tests or application code. Set `SKRIFT_WORKERS_REDIS_URL` to route worker Redis backends to a dedicated Redis instance while leaving the rest of the app on `redis.url`. SQLAlchemy backends use the configured database session maker.

## Persistence

The persister copies hot-path worker data into cold storage.

Use `stream_prefixes` for dynamic stream families. For example,
`stream_prefixes: ["agents:run"]` archives per-session agent audit streams such
as `agents:run:<session_id>` without needing to know session IDs ahead of time.

| Option | Default | Description |
|--------|---------|-------------|
| `streams` | `["workers:lifecycle"]` | Event streams copied into the archive |
| `stream_prefixes` | `[]` | Event stream prefixes discovered and copied into the archive |
| `batch_size` | `100` | Maximum events flushed per stream per pass |
| `flush_interval` | `1.0` | Seconds between event flush passes |
| `snapshot_keys` | `["workers:queue_wait_history"]` | Exact state keys snapshotted into the archive |
| `snapshot_prefixes` | `[]` | State key prefixes snapshotted into the archive |
| `snapshot_interval` | `60.0` | Seconds between snapshot passes |

Run the persister continuously:

```bash
skrift workers persister
```

Run one pass:

```bash
skrift workers persister --once
```

`persister --once` flushes configured event streams, snapshots configured state keys, and runs pruning. `skrift workers prune` runs only retention pruning.

## Retention

Retention pruning keeps hot-path stores and archives bounded.

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Start pruning inside `skrift workers persister` |
| `prune_interval` | `300.0` | Seconds between pruning passes |
| `terminal_job_state_ttl` | `604800` | Age in seconds before completed, failed, cancelled, or dead-lettered Redis job state can be removed |
| `redis_event_ttl` | `86400` | Minimum age before archived Redis stream events can be removed |
| `redis_event_max_entries` | `100000` | Maximum Redis stream entries retained per stream after archive cursor safety checks |
| `dead_queue_marker_ttl` | `86400` | Age before Redis dead queue markers can be removed |
| `archive_event_ttl` | `7776000` | Age before SQLAlchemy archive events can be removed |
| `archive_snapshot_ttl` | `2592000` | Age before SQLAlchemy archive snapshots can be removed |
| `dlq_resolved_ttl` | `2592000` | Age before replayed or discarded DLQ entries can be removed |

Manual pruning:

```bash
skrift workers prune --json
```

Redis lifecycle events are pruned only after the persister cursor shows they have been archived.

The defaults keep Redis hot-path data long enough for fast recent `jobs inspect` and admin views while keeping longer operational history in SQLAlchemy archive and DLQ tables. Individual TTLs cannot be disabled; each TTL field must be a positive number. To disable pruning, set `workers.retention.enabled: false`.

## Dead-Letter Queue

DLQ entries use `DeadJobEntry` records with a structured `cause` and `state`.

| Cause | Meaning |
|-------|---------|
| `retries_exhausted` | The handler failed until attempts were exhausted |
| `permanent_failure` | The handler raised `PermanentFailure` |
| `reclaim_loop` | The queue claim expired too many times |
| `poison` | Payload validation failed before execution |

| State | Meaning |
|-------|---------|
| `open` | Available for operator action |
| `replayed` | Retried as a new job |
| `discarded` | Marked resolved without retrying |

`dlq retry` and `dlq discard` accept explicit entry IDs or filters. Filtered actions default to `state=open`; pass `--state` to target another state. `permanent_failure` and `poison` retries require `--force`.

## Custom Backends

Use `tests/test_worker_backend_contracts.py` as the compatibility suite for new backend implementations.

Custom backend classes are loaded from the import strings in `workers.backends`. During instantiation, Skrift passes `settings` if the constructor accepts it and passes `session_maker` if the constructor accepts it.

Required protocol methods:

```python
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any

from skrift.workers.models import ClaimedJob, DeadJobEntry, JobEnvelope, QueueStats

UpdateFn = Callable[[Any], Any | Awaitable[Any]]


class StateStore:
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, *, ttl: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def update(self, key: str, fn: UpdateFn, *, ttl: float | None = None) -> Any: ...
    async def keys(self, prefix: str = "") -> list[str]: ...
```

```python
class EventLog:
    async def append(self, stream: str, event: dict[str, Any]) -> int: ...
    async def read(self, stream: str, *, from_position: int = 0, limit: int | None = None) -> list[tuple[int, dict[str, Any]]]: ...
    async def read_filtered(self, stream: str, *, filters: dict[str, Any], from_position: int = 0, limit: int | None = None) -> list[tuple[int, dict[str, Any]]]: ...
    async def subscribe(self, stream: str, *, from_position: int | None = None) -> AsyncIterator[tuple[int, dict[str, Any]]]: ...
    async def delete(self, stream: str) -> None: ...
```

```python
class Queue:
    async def submit(self, job: JobEnvelope) -> None: ...
    async def claim(self, queues: list[str], *, visibility_timeout: float) -> ClaimedJob | None: ...
    async def ack(self, queue: str, job_id: str, token: str) -> None: ...
    async def nack(self, queue: str, job_id: str, token: str, *, retry_at: datetime | None = None, dead_letter: bool = False) -> None: ...
    async def cancel(self, queue: str, job_id: str) -> bool: ...
    async def wake(self, queue: str, job_id: str, *, resume_at: datetime | None = None) -> bool: ...
    async def stats(self, queue: str) -> QueueStats: ...
```

```python
class DeadLetterStore:
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
```

```python
class Archive:
    async def bulk_insert_events(self, events: list[tuple[str, int, dict[str, Any]]]) -> None: ...
    async def upsert_state_snapshot(self, key: str, value: Any, *, timestamp: datetime | None = None) -> None: ...
    async def query_events(self, stream: str, *, from_position: int = 0, to_position: int | None = None) -> list[tuple[int, dict[str, Any]]]: ...
    async def latest_state_snapshot(self, key: str) -> Any: ...
    async def historical_state_snapshots(self, key: str) -> list[tuple[datetime, Any]]: ...
```

Optional methods improve admin views and retention:

| Backend | Optional method | Used for |
|---------|-----------------|----------|
| State store | `worker_job_states(limit=None)` | Efficient job listing |
| State store | `worker_job_counts()` | Efficient active/total counts |
| State store | `prune_terminal_job_states(max_age_seconds=...)` | Retention |
| Event log | `read_tail(stream, limit=...)` | Efficient recent event display |
| Event log | `completed_job_history(hours=..., bucket_count=...)` | Admin charts |
| Event log | `prune_archived_events(...)` | Redis hot-path retention |
| Queue | `prune_dead_markers(max_age_seconds=...)` | Redis dead marker retention |
| Dead-letter store | `summary()` | Efficient DLQ summary |
| Dead-letter store | `prune_resolved(max_age_seconds=...)` | Retention |
| Archive | `prune(event_max_age_seconds=..., snapshot_max_age_seconds=...)` | Archive retention |

Optional admin methods fall back to slower scans or generic summaries when absent. Optional retention methods are skipped when absent, so a backend can still satisfy the core protocol without supporting pruning hooks.

## CLI Reference

| Command | Purpose |
|---------|---------|
| `skrift workers run` | Run a standalone worker process |
| `skrift workers persister` | Run event flushing, state snapshots, and retention pruning |
| `skrift workers prune` | Run one pruning pass |
| `skrift workers queues list` | Show queue depth and age |
| `skrift workers jobs inspect JOB_ID` | Show job state and lifecycle events |
| `skrift workers dlq list` | List dead-letter entries |
| `skrift workers dlq inspect ENTRY_ID` | Show one DLQ entry |
| `skrift workers dlq retry [ENTRY_ID...]` | Replay one or more DLQ entries, or a filtered set, as new jobs |
| `skrift workers dlq discard [ENTRY_ID...]` | Mark one or more DLQ entries, or a filtered set, discarded |
| `skrift workers dlq export` | Export DLQ entries as JSON |

All process-oriented commands reject memory backends by default because process-local data cannot be shared. Use `--allow-memory-backends` only for local tests.
