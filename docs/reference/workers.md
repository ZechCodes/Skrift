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
  max_inflight_per_worker: 1
  poll_interval: 0.1
  max_poll_interval: 2.0
  poll_backoff_factor: 2.0
  visibility_timeout: 30.0
  reaper_interval: 5.0
  max_reclaims: 3
  drain_timeout: 20.0
  drain_cancel_timeout: 5.0
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
| `max_inflight_per_worker` | `1` | How many claimed jobs each worker runs at once. See [Several jobs per worker](#several-jobs-per-worker) |
| `poll_interval` | `0.05` | Base seconds a worker waits after an empty claim; the floor an idle worker collapses back to the moment a claim succeeds |
| `max_poll_interval` | `2.0` | Ceiling for the idle poll backoff. After each empty claim the wait grows by `poll_backoff_factor` up to this value, so idle queues stop polling the database at full frequency |
| `poll_backoff_factor` | `2.0` | Growth factor applied to the poll interval on each consecutive empty claim. `1.0` disables backoff and keeps polling at `poll_interval` |
| `visibility_timeout` | `30.0` | Seconds before an unacked claim can be reclaimed. A job whose own `visibility_timeout` (set on `@handler` or at submit) is longer keeps its claim for that long instead; a job that sets none uses this value |
| `reaper_interval` | `5.0` | Seconds between runs of the standalone reaper that reclaims expired claims and sweeps expired state, decoupled from poll frequency |
| `max_reclaims` | `3` | Number of claim timeouts allowed before dead-lettering as a reclaim loop |
| `drain_timeout` | `20.0` | Seconds a stopping worker gives its running jobs to finish before cancelling them and handing their claims back. See [Stopping a worker](#stopping-a-worker) |
| `drain_cancel_timeout` | `5.0` | Seconds a cancelled job's handler then gets to stop before the worker leaves it behind with its claim, to expire |
| `governor` | none | `module:attribute` of a callable asked before each claim whether the worker takes another job. See [Gating claims with a governor](#gating-claims-with-a-governor) |
| `imports` | `[]` | Modules imported by standalone worker processes and app startup to register handlers |

#### Several jobs per worker

By default each worker runs one job at a time, so a pool runs at most `concurrency` jobs at once. Jobs that spend most of their time waiting on I/O, such as an agent run waiting on a model or a scraper waiting on HTTP, leave their worker idle meanwhile. Set `max_inflight_per_worker` above 1 to let each worker keep claiming while its jobs wait: a pool then runs up to `concurrency × max_inflight_per_worker` jobs at once.

- **One poller per worker.** A worker's free places take turns to poll, one claim at a time, and share the idle backoff. An idle worker queries the queue exactly as often as one running a job at a time, however many places it has. A worker whose places are all busy doesn't poll.
- **Each job keeps its own claim.** The place that claims a job runs it, then acks or nacks it, as a single-job worker would.
- **Ordering.** Jobs claimed by one worker can finish, and be acked, out of claim order. Keep the default of 1 if a queue's jobs must run one after another.
- **A blocked event loop blocks every place.** Places share the worker's event loop. A handler that blocks it (synchronous I/O, heavy CPU work) stalls every job the worker has in flight, and their claims can expire, letting another worker run them a second time. Only raise this for handlers that `await` their waits, and size `visibility_timeout` for the slowest job.
- **Awaited sub-agents** count places, not workers; see [Sub-agents on the in-process worker pool](agents.md#sub-agents-on-the-in-process-worker-pool).

#### Gating claims with a governor

`max_inflight_per_worker` is a fixed ceiling. A governor is a gate that moves at runtime: back-pressure from a downstream API, memory pressure, a cost budget. Before each claim, the worker asks it whether to take another job now:

```python
# myapp/governors.py
async def should_claim_next(runtime, current_inflight: int) -> bool:
    return current_inflight < await downstream_capacity()
```

```yaml
workers:
  max_inflight_per_worker: 8
  governor: myapp.governors:should_claim_next
```

- **When it's asked.** Inside the worker's poll turn, just before the claim: once per poll per worker, never per place. `True` lets the worker claim. `False` claims nothing and waits as an empty poll does, sharing the [idle poll backoff](#idle-poll-backoff). After a no, the worker asks again within the current backoff interval (up to `max_poll_interval`) plus the time the governor takes to answer. A successful claim resets the backoff.
- **The cap still binds.** A worker whose places are all busy doesn't poll, so it never asks. The more restrictive of the cap and the governor wins.
- **`current_inflight`** is how many of *this worker's* places hold a claimed job, from the claim to its settlement. That includes a parent run waiting on a sub-agent. It is a local hint, not a count for the pool, the process or the cluster, and not a distributed budget: every worker calls the same governor, concurrently. A limit across processes needs an atomic admission decision of its own, such as a Redis counter. A `True` may also be followed by an empty or failed claim, and there is no hook to hand back a reservation.
- **Answers.** It must return (or, if async, resolve to) an actual `bool`. `None`, numbers and other truthy objects count as a no. So does a governor that raises: the worker **fails closed** and claims nothing until the governor answers again. The first failure in a streak is logged at warning, and the next `True` or `False` is logged at info as the end of the streak.
- **Sync or async.** Either works. A sync governor runs on the event loop, so a slow one stalls every place of every worker, like any [blocking handler](#several-jobs-per-worker).
- **Where it applies.** Only where a pool claims: `in_process` execution in the web app, and `skrift workers run`. Both import the path once at startup, so a bad path fails the start, not the first poll. Inline execution runs jobs without claiming, so it never asks.
- **Awaited sub-agents can deadlock.** A parent run that awaits a queued sub-agent holds its place until the sub-agent finishes, and the sub-agent needs a claim. A governor that refuses while the parent waits stalls both. For example, with one parent in flight and a governor that allows `current_inflight < 1`, the child is refused forever, even though the worker has free places. The [free-place check](agents.md#sub-agents-on-the-in-process-worker-pool) counts places, not governor answers, so it can't catch this. Allow for waiting parents in the governor's rule. Dispatching such sub-agents inline avoids the dependency, but inline runs are never asked, so they don't keep a strict resource limit either.
- **Stopping.** A stopping worker doesn't ask. A governor mid-answer when a stop begins is cancelled, and whatever it returns then admits nothing. This holds from the moment the pool starts its drain; `WorkerRuntime.stop()` first finishes its background tasks, and a claim can still be admitted until then. The drain relies on the governor letting its `CancelledError` propagate; see [Stopping a worker](#stopping-a-worker).

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

#### Stopping a worker

`skrift workers run` stops on SIGTERM or SIGINT, and the web app's in-process pool stops at shutdown. Either way the pool drains:

1. **It claims nothing new.** A worker between polls stops at once. A worker already claiming runs whatever that claim returns as part of the drain, and one still waiting on its claim when the window ends is cancelled.
2. **Running jobs get `drain_timeout` to finish.** A job that finishes settles as usual: acked, retried, paused or dead-lettered.
3. **Jobs still in their handler are then cancelled and handed back.** The worker releases each claim so it is visible again at once, and sets the job's state back to `submitted`, keeping any `paused_state` it resumed from. If the queue fails to take a job back (its `nack` raises), the worker puts the job's `running` state back, logs an error with the job id, and abandons the job with its claim held; another worker runs it once the claim expires. A hand-back charges no attempt and is not a reclaim, so it never counts toward `max_attempts` or `max_reclaims`. A handler that turns its `CancelledError` into another exception is still handed back rather than failed; one that swallows it and returns a result completes normally. A job whose claim reaches the handler only after the drain window is handed back without running.
4. **A handler still running `drain_cancel_timeout` later is abandoned.** A handler that swallows its `CancelledError` cannot be stopped, so it is left running with its claim, which expires; another worker then runs the job. An agent run's claim on the in-memory queue stops being renewed at that point, so it expires too. Its state reads `running` until then. The worker logs a warning with the job id. An abandoned run settles nothing, however its handler later ends: no `ack` or `nack`, no state write, only a warning. So a process that exits while the handler finishes can never leave the job acked but still `running`. A handler that ends while the drain is still waiting on other jobs, before its run was abandoned, settles as usual, and the stop waits for it.

`WorkerRuntime.stop()` returns the ids of the jobs it abandoned, and so does every later call until the runtime starts again. A cancelled `stop()`, such as a server cancelling its shutdown, still drains the pool and waits for the drain before it raises `CancelledError`. When any jobs were abandoned, `skrift workers run` logs them at warning and exits directly, with the exit code it would have used, without waiting for the abandoned handlers: `asyncio.run` would otherwise wait for them forever. The web app's in-process pool cannot do that, because the server owns the event loop; there an abandoned handler can hold the process's exit until the orchestrator kills it at the end of its grace period.

Stopping takes at most `drain_timeout + drain_cancel_timeout`, 25 s by default, which is under Kubernetes' default `terminationGracePeriodSeconds` of 30. Raise the grace period if you raise either timeout. There are two exceptions:
- **The worker's own writes for a job.** A stop never cuts its `ack`, `nack` or state writes short, and waits for any that have started, so a backend that hangs on a write can hold a stop past that bound.
- **A [governor](#gating-claims-with-a-governor) that doesn't let cancellation through.** The drain cancels a governor that is answering, and waits for it. One that catches the `CancelledError` and goes on waiting holds the stop for as long as it waits. A governor must therefore let `CancelledError` propagate. It must never await `WorkerRuntime.stop()` either, since the stop would then wait on the governor's own place.

A handed-back job runs again from the start on another worker. Anything it did before it was cancelled has already happened, as with any retry.

Agent runs are jobs too:
- **A run awaiting a sub-agent** keeps its worker and its claim through the drain window. With the in-memory queue the claim stays renewed (see [Sub-agents on the in-process worker pool](agents.md#sub-agents-on-the-in-process-worker-pool)). If the sub-agent finishes inside the window, the parent finishes too.
- **A sub-agent a draining pool will not run** means its parent cannot finish. That is a sub-agent that is still queued, since the draining pool claims nothing, or one still running when the window ends. The parent is then handed back with the rest. The sub-agent's own job is left queued, or is handed back itself if it was running. On the successor the parent re-runs its tool call and dispatches a new sub-agent session, while the first sub-agent's job also runs to completion.

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

### When the DLQ record fails to save

A job's state is set to `DEAD_LETTERED`, and its queue entry dead-lettered, before its DLQ record is created. The runtime then writes the record to the state store as a pending marker (`workers:dead_letter_pending:JOB_ID:ENTRY_ID`), creates the record, emits `job_dead_lettered`, calls the handler's dead callback, and deletes the marker. If the dead-letter store fails, the job has no DLQ record yet, and `dlq retry` cannot replay it:
- The runtime logs this at error level, with the exception. The message names the job id, queue, job type and cause, says the job has no dead-letter record, and says whether its pending marker was saved.
- The same fields are on the log record as `job_id`, `queue`, `job_type` and `cause`, for structured log handlers.
- The exception is still raised. A worker's loop logs it and goes on, and an inline or poison submission raises it to the caller.
- No `job_dead_lettered` event is emitted, and the handler's dead callback is not called.

`skrift workers dlq reconcile` (or `WorkerRuntime.reconcile_dead_letters()`) finishes every dead letter whose marker is still stored: it creates the record unless one with the same entry id exists, emits `job_dead_lettered`, calls the dead callback, and deletes the marker. A marker that fails again is kept and reported, and the command exits 1. So is a job whose handler is not registered in the reconciling process: its record is created, but the event and callback wait, with the marker, until the handler is registered. Reconciling also finishes a dead letter whose process stopped between writing the marker and deleting it.

Every worker start runs one reconcile pass in the background once its pool is polling, so a slow store does not hold up the start and a dead callback can submit and wait on queued jobs. A failure there is logged. Stopping the worker cancels a pass still running; its unfinished markers stay for the next one.

Concurrent reconciles, such as two workers starting at once, or a reconcile racing the dead letter itself, create one record: whichever creates second finds the record and counts it as created, but the `job_dead_lettered` event and the dead callback are at-least-once: a dead letter can deliver them more than once, so callbacks should tolerate a repeat.

A process that stops after writing the job's `DEAD_LETTERED` state but before writing its marker leaves neither a marker nor a record, and reconciling cannot find the job. Nor can it recover a job whose marker failed to save (the log says so). For either, find the job's state by job id (`skrift workers jobs inspect JOB_ID`). The state store keeps the job's envelope, its attempts and its error; resubmit the job from them if it should run again.

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

`update` must run `fn` and write its result so that no other `update` of the key lands between the read and the write; `fn` may refuse by raising, and then nothing is written. `set` and `delete` are not ordered against `update`: one can land between an update's read and its write, which then overwrites the set value or recreates the deleted one. On Postgres, a `set` of a missing key racing a first `update` of it can also fail with a uniqueness error once the update commits. So the runtime writes worker job state (`workers:jobs:*`) and agent run state only through `update`. `set` is left to single-writer keys: the queue wait and trend histories (`workers:queue_wait_history`, `workers:queue_trend_history`) and the event archive's flush cursors. No in-tree code calls `delete`; a backend's own pruning of job state (`prune_terminal_job_states`) must take the key's update lock and recheck the state before deleting it.

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

`create` must raise when an entry with the same `id` is already stored, never overwrite it: reconciling relies on that, so a record an operator has already replayed or discarded is not reset to `open`.

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
| `skrift workers dlq reconcile` | Recreate DLQ records that failed to save |

All process-oriented commands reject memory backends by default because process-local data cannot be shared. Use `--allow-memory-backends` only for local tests.
