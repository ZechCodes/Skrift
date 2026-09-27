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
  max_poll_interval: 2.0
  poll_backoff_factor: 2.0
  visibility_timeout: 30.0
  reaper_interval: 5.0
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
    terminal_runstate_ttl: 86400
    active_runstate_ttl: 604800
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
| `poll_interval` | `0.05` | Base seconds a worker waits after an empty claim; the floor an idle worker collapses back to the moment a claim succeeds |
| `max_poll_interval` | `2.0` | Ceiling for the idle poll backoff. After each empty claim the wait grows by `poll_backoff_factor` up to this value, so idle queues stop polling the database at full frequency |
| `poll_backoff_factor` | `2.0` | Growth factor applied to the poll interval on each consecutive empty claim. `1.0` disables backoff and keeps polling at `poll_interval` |
| `visibility_timeout` | `30.0` | Seconds before an unacked claim can be reclaimed. A job whose own `visibility_timeout` (set on `@handler` or at submit) is longer keeps its claim for that long instead; a job that sets none uses this value |
| `reaper_interval` | `5.0` | Seconds between runs of the standalone reaper that reclaims expired claims and sweeps expired state, decoupled from poll frequency |
| `max_reclaims` | `3` | Number of claim timeouts allowed before dead-lettering as a reclaim loop |
| `imports` | `[]` | Modules imported by standalone worker processes and app startup to register handlers |

#### Idle poll backoff

An idle worker no longer polls at a flat `poll_interval`. After each empty claim it multiplies its wait by `poll_backoff_factor` up to `max_poll_interval`, then collapses straight back to `poll_interval` the moment a claim succeeds. This trades a little pickup latency on the first job after an idle stretch (up to `max_poll_interval`) for a large drop in database query volume while queues are empty. Lower `max_poll_interval` if you need snappier pickup on quiet queues; raise it to quiesce the database further. The reaper that reclaims expired claims runs on its own `reaper_interval` cadence, so backing off polling never delays reclaims.

If you are on an older release and need to blunt idle query volume immediately, raising `poll_interval` (for example to `1.0`) cuts the spin without deploying, at the cost of uniform pickup latency; the backoff above makes that stopgap unnecessary.

#### When a claim expires mid-run

A handler that outlives `visibility_timeout` (slow work, a blocked event loop, a paused process) loses its claim. The reaper releases it, and another worker can claim and run the job while the first handler is still going. When the first handler finally returns, raises or pauses, its worker records nothing for the job:
- Its `ack` or `nack` is refused, because the queue checks the claim token atomically with the write.
- It emits no lifecycle events and creates no dead letter.
- It leaves the job's state to the new run. Each run tags the state it writes with its claim's order, and a later claim always wins: a run's RUNNING state and the writes it makes before its `ack` or `nack` (a retry's error, a pause) land only while the stored state is this run's own or was written by an earlier claim's run. If the claim then turns out to be lost, the state is put back to what it was before the run started. The order is a count of the job's claims that the queue keeps with the job (a `claim_generation` column on SQLAlchemy, a field in the stored envelope on Redis, a counter in memory), not a clock reading, so clock steps cannot reorder claims.
- Once the queue accepts a run's `ack` or dead-letter `nack`, that run owns the job, so its completed or dead-lettered state is written whatever is stored.

A worker that only reaches a claim after it expired (a blocked event loop between claiming and starting) skips the run without writing anything, judged by when it received the claim and the lease the queue reported. One whose start arrives after the job was claimed again skips it too. A warning is logged instead of a worker-loop error.

This protects Skrift's own records only. Anything the stale handler did outside them (external API calls, emails, its own database writes) already happened, and guarding those against a concurrent second run is the handler's responsibility. Size `visibility_timeout` (per handler where needed) above your handler's worst-case run time.

The run-id check is a single atomic state-store update on Postgres and the in-memory store. On SQLite and Redis, `update` is not yet atomic against a plain concurrent write (#195), so a narrow window remains there.

The SQLAlchemy queue keeps the order in a new `worker_queue.claim_generation` column: run `skrift db upgrade head` before starting workers on this release. Jobs already queued start from 0.

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
| `terminal_runstate_ttl` | `86400` | TTL applied to an agent session's hot `RunState` once it reaches a terminal status (completed, failed, cancelled); the terminal state remains durable in the archive snapshot |
| `active_runstate_ttl` | `604800` | Sliding TTL refreshed on every write to a non-terminal `RunState`, so a wedged session cannot leak indefinitely |
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

The defaults keep Redis hot-path data long enough for fast recent `jobs inspect` and admin views while keeping longer operational history in SQLAlchemy archive and DLQ tables. Each TTL field must be a positive number and cannot be disabled, with one exception: `active_runstate_ttl` may be set to `null` to leave non-terminal agent sessions without a sliding TTL (terminal sessions still expire via `terminal_runstate_ttl`). To disable pruning entirely, set `workers.retention.enabled: false`.

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
    async def nack(self, queue: str, job_id: str, token: str, *, retry_at: datetime | None = None, dead_letter: bool = False, job: JobEnvelope | None = None) -> None: ...
    async def cancel(self, queue: str, job_id: str) -> bool: ...
    async def wake(self, queue: str, job_id: str, *, resume_at: datetime | None = None) -> bool: ...
    async def stats(self, queue: str) -> QueueStats: ...
```

`claim` must hold the claim for the longer of its `visibility_timeout` argument (the worker's `workers.visibility_timeout`) and the claimed job's own `visibility_timeout`. A job's `visibility_timeout` of `None` means it sets none, and the argument alone applies.

The runtime passes the claimed envelope to `nack` as `job`; store it in place of the queued copy so the incremented `attempt` survives the retry, otherwise `max_attempts` never dead-letters.
A `nack` without the `job` parameter is deprecated: the runtime still calls it without the envelope and emits a `DeprecationWarning`, but such a queue cannot persist attempts.
Set `visibility_timeout` on the returned `ClaimedJob` to the seconds the claim is held for (that longer of the two, not the argument); a worker that reaches a claim after that has passed skips it instead of starting a run the queue may already have handed to another worker. Leaving it `None` disables that check. Set `claim_order` to a value that is greater for each later claim of a job and never repeats, kept with the job so it is lost only together with it; do not derive it from a clock, which can step backwards. Without it (`None`) the runtime does not order that queue's claims, so a worker whose claim was taken over can still write over the later claim's state.
`ack` and `nack` must check the claim token in the same atomic step as the write (a conditional `DELETE`/`UPDATE`, a row lock, or a server-side script; a lock with a timeout is not enough on its own) and raise `ValueError` when it no longer matches. Once a claim expires, the reaper in another worker process can release it and a second worker can claim the job; a late `ack` or `nack` from the first worker must then change nothing.

`wake` of a claimed job (running, or pausing before its `nack` lands) must not disturb the claim. Record it on the claim instead and return `True`: the worker's `nack` then makes the job ready at the recorded time rather than its own retry time, atomically with releasing the claim. Keep only the latest such wake. `ack`, dead-lettering and a reaped claim drop it.

The built-in SQLAlchemy and Redis queues set and judge claim leases on the server's clock (`clock_timestamp()` on Postgres, `TIME` on Redis), so worker hosts with skewed clocks agree on when a claim expires. When a job becomes ready (`scheduled_for`, retry and wake times) is still judged on the host clock that wrote it. SQLite uses the local clock throughout, which every process sharing the file already shares. Upgrading from a release before this change: until every worker process runs the new reaper, an old one can still overwrite a newer claim, so a rolling deploy carries that risk until it completes.

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
