# Skrift Workers — Design Outline

A handoff spec for the coding agent. This document describes **what needs to exist** and the **shape** of each component for Skrift's worker subsystem. It does not specify implementations or third-party integrations; the coding agent decides those.

---

## 1. Scope and framing

This work introduces a new Skrift subsystem: **workers**. A generic substrate for durable, optionally-out-of-process background work.

Workers is a generic concept. It has multiple consumers — agents (specified separately), email retries, scheduled site rebuilds, webhook delivery, image processing, search index updates, and similar concerns. The subsystem must be designed for generality from day one. No consumer is privileged in the design.

**Core principle:** every backend is pluggable behind an interface, so the same code runs in a single Python process with SQLite *and* across a fleet of processes with Redis + Postgres. The framework selects implementations based on configuration.

---

## 2. Operating modes

The subsystem must explicitly support three modes. The coding agent should treat all three as first-class; none should be a degraded experience.

### Local
Single process, no external services. Used for development, testing, small sites, REPL exploration. All backends in-memory or SQLite-backed. Handlers execute either inline (synchronous) or as in-process background tasks. No separate processes.

### Single-node
One machine, possibly multiple processes (web + workers), but no horizontal scaling. Backends may be in-memory, SQLite, or Redis depending on which processes need to share state. Common production shape for a side project or early-stage product.

### Distributed
Separate web, worker, and persister processes, possibly across machines. Redis for hot path (state, queue, event log), Postgres for archive. Standard production shape.

The user/operator selects the mode via configuration. Code authored for one mode runs in all three without changes.

---

## 3. Backend interfaces

Four pluggable backend interfaces. Each is a protocol/abstract class with a documented contract; concrete implementations live in `skrift.backends.*` modules. The framework only depends on the interfaces.

### 3.1 `StateStore`
**Responsibility:** key/value storage with atomic update semantics, optional TTL, prefix scans.

**Required operations (shape, not signatures):**
- Get / set / delete by key
- Compare-and-swap or transactional update (for safe concurrent state mutation)
- List keys by prefix (for housekeeping, not hot path)
- Optional TTL on writes

**Required implementations:** in-memory (dict + asyncio.Lock), SQLite, Redis.

### 3.2 `EventLog`
**Responsibility:** append-only ordered log, partitioned by stream key. Supports both replay-from-cursor and live tail subscription. Used by the worker subsystem itself for lifecycle events, and by consumers (e.g., agents) for their own event streams.

**Required operations:**
- Append event to a named stream, returning a monotonic position
- Read range from a stream (replay)
- Subscribe to a stream from a cursor and receive new events as they land (live tail)
- Stream lifetime management (truncation policy, deletion)

**Required implementations:** in-memory (asyncio queues + list), Redis Streams. SQLite is optional but useful for local mode if events are wanted across process restarts.

This subsumes pub/sub for downstream use cases — subscribing from the current tip is the live case, subscribing from position 0 is the replay case.

### 3.3 `Queue`
**Responsibility:** durable work queue with claim/ack semantics, visibility timeouts, and retry support. Distinct from EventLog because semantics differ: queues care about exactly-one-consumer-claims-it and recovery from worker death; event logs care about fan-out and ordered replay.

**Required operations:**
- Submit job (with optional delay / scheduled time)
- Claim next job from a named queue with a visibility timeout
- Acknowledge completion
- Negative-acknowledge with retry policy (immediate, backoff, dead-letter)
- Reclaim expired (worker-died) claims
- Inspect queue state for ops/observability

**Required implementations:** in-memory (asyncio), Redis Streams with consumer groups. Named queues are mandatory — different work classes live in different queues with different worker pools.

### 3.4 `Archive`
**Responsibility:** relational, durable, batch-friendly cold storage for events and state snapshots. Source of truth for replay and audit; not on the hot path.

**Required operations:**
- Bulk insert events
- Upsert state snapshot by key
- Query event range by stream + position window
- Query latest state snapshot by key
- Query historical state snapshots (for time-travel debugging)

**Required implementations:** SQLite, Postgres. Schema lives here, not in StateStore.

### 3.5 Notes on the abstraction layer
- All backends are async.
- Backends do not know about jobs, handlers, or any consumer's domain — they deal in keys, streams, queues, and rows. Domain types live in the layers above.
- Backends declare capabilities (e.g., "supports TTL," "supports transactions"). Higher layers can branch on capabilities or fail closed if a required capability is missing in the chosen backend.
- The framework ships a smoke-test suite that any backend implementation must pass. New backends are added by passing the suite, not by reading docs.

---

## 4. Job

A Pydantic model representing a unit of work. Each job has:
- A type discriminator (string name)
- A typed payload (Pydantic fields)
- Metadata: id, queue name, submitted_at, attempt count, max attempts, visibility timeout, scheduled-for time
- Optional correlation id for tracing
- Optional parent job id for lineage

Jobs are serialized to/from the Queue backend.

---

## 5. Handler registry

A global registry mapping job type names to handler functions. Handlers are registered via decorator at module import time. Each handler declares:
- The job type it handles
- The expected payload model
- Default queue name
- Default retry/timeout policy (overridable per submission)

The registry is the discovery mechanism for worker processes — they import the module that registers handlers, then claim jobs whose types they recognize.

---

## 6. Worker pool

A `WorkerPool` runs N concurrent workers within a single process. Each worker:
- Claims a job from one or more queues
- Looks up the handler by job type
- Reconstructs the typed payload
- Executes the handler with appropriate context
- Handles the result: ack on success, nack with policy on failure, re-enqueue on cooperative pause

The pool is configurable for:
- Concurrency (how many workers in this process)
- Queue subscriptions (which queues this pool drains, with optional weighting)
- Heartbeat/visibility behavior

---

## 7. Execution modes for handlers

A worker pool can run in three modes, selected at config time:
- **Inline** — `submit()` runs the handler synchronously in the calling task. For tests and the simplest local mode.
- **In-process** — handlers run as asyncio tasks in the same process as the submitter. Visibility timeouts still apply (in case the task is cancelled).
- **Out-of-process** — handlers run in a separate worker process; submitter and worker communicate via Queue/StateStore/EventLog. The production shape.

The handler code is identical across all three. The worker pool's mode is an operational concern.

---

## 8. Job lifecycle and reliability

The worker subsystem must implement:

- **Visibility timeouts** with auto-reclaim of dead workers' claims.
- **Configurable retry policies** (max attempts, backoff function, jitter).
- **Dead-letter destination** for terminally-failed jobs. Inspectable and re-submittable.
- **Scheduled / delayed jobs** — a job becomes claimable at a future time.
- **Cooperative pause** — a handler can voluntarily yield control and re-enqueue itself with updated state, without consuming a retry attempt. The handler's continuation is gated on an external trigger (e.g., another submission against the same correlation id, or a scheduled wake-up). This is the primary mechanism complex consumers will use for human-in-the-loop pauses, time-slicing long-running work, and waiting on external signals.
- **Cancellation** — a submitted job can be cancelled before claim; in-flight cancellation is best-effort and surfaced via a cancellation signal the handler may check.

---

## 9. Job handle

`submit()` returns a `JobHandle`:
- Awaitable — resolves with the handler's return value when the job completes (or raises on terminal failure)
- Status-queryable — current state, attempt count, last error
- Cancellable
- Can be reconstructed from a job id alone (so a different process can wait on the same job)

The job handle is the primitive that higher-level abstractions (e.g., agent sessions) build on top of.

---

## 10. Worker lifecycle events

The subsystem emits its own lifecycle events on the EventLog so external observability can subscribe:

- `job_submitted`
- `job_claimed`
- `job_started`
- `job_paused` (cooperative pause)
- `job_resumed`
- `job_completed`
- `job_failed`
- `job_dead_lettered`
- `job_cancelled`

Each event carries job id, queue, type, attempt count, and timestamp. Consumers (e.g., a health dashboard) subscribe via the standard EventLog tail mechanism.

These events are distinct from any domain events that handlers themselves emit. Handlers may emit their own events on their own EventLog streams; the worker subsystem does not interpret them.

---

## 11. Persistence

The worker subsystem provides two background persistence services as generic infrastructure. Either can run as a dedicated process or embedded in another process, depending on operating mode.

### 11.1 Event flusher
Drains EventLog into Archive in batches. Configurable batch size and flush interval. Tracks its position per stream so it can resume after restart without dropping or duplicating events. Operates on any stream — worker lifecycle events, handler-domain events, doesn't matter.

### 11.2 State snapshotter
Periodically writes selected StateStore keys into Archive as historical snapshots. Frequency configurable per key class. Supports time-travel queries (state-as-of-timestamp) on the Archive.

Whether handlers use state snapshotting is up to them — the snapshotter is opt-in per state class.

---

## 12. User-facing surface

The shape of what users import and call. Concrete signatures are for the coding agent to design.

```
skrift.Job             — base for typed worker jobs
@skrift.handler(...)   — register a handler for a job type
skrift.submit(...)     — submit a job, get a JobHandle
skrift.JobHandle       — awaitable / queryable handle (rarely instantiated directly)
skrift.configure(...)  — set up backends and operating mode
skrift.local_executor  — context manager for in-process worker mode
```

That is the entire user-facing surface for the worker subsystem itself. Everything else is internal or belongs to consumers.

---

## 13. Configuration

A `SkriftConfig` Pydantic model selects backend implementations and operating mode. Three preset configurations should be shipped:

- **`local`** — in-memory backends, inline or in-process workers, SQLite archive.
- **`single_node`** — Redis or in-memory hot path, SQLite or Postgres archive, in-process workers.
- **`distributed`** — Redis hot path, Postgres archive, out-of-process workers, separate persister.

Presets are starting points; every backend choice and worker setting is independently overridable. The config is validated at startup; invalid combinations (e.g., out-of-process workers with an in-memory queue) fail fast with a clear error.

---

## 14. Runtime processes and CLI

Skrift gains a `skrift workers` command group:
- `skrift workers run` — start a worker process. Flags select queues, concurrency, pool name.
- `skrift workers persister` — start the event flusher and state snapshotter as a dedicated process.
- `skrift workers queues list` — inspect queue depth and consumer state.
- `skrift workers jobs inspect <job-id>` — inspect a job's lifecycle and history.
- `skrift workers dlq` — list and re-submit dead-lettered jobs.

In local mode none of these are required; everything runs in the application process.

---

## 15. Open design questions

Things the coding agent should make explicit decisions on, ideally early.

1. **Persister deployment.** Always a separate process vs. optionally embedded in web/worker processes. Lean: embeddable in local and single-node modes, separate in distributed.
2. **EventLog vs. Queue unification.** Both can be implemented over Redis Streams; the contracts differ. Worth keeping separate interfaces even if a backend reuses one primitive — the semantics shouldn't leak.
3. **Cooperative pause re-enqueue trigger.** Is the wake mechanism (a) re-submitting against the same correlation id, (b) a separate `wake(job_id)` API, or (c) the handler returning a "wake when X" descriptor? This affects the contract for any consumer using pauses (HITL, external waits).
4. **Backend capability discovery.** How a higher layer queries "does my backend support X" — runtime introspection, declared capability set, or fail-on-use. Lean: declared capability set with startup validation.
5. **Job deduplication.** Whether the queue supports submission-time dedup (same correlation id within a window). Useful for idempotent re-submission, adds complexity. Lean: not in v1; consumers handle dedup at their layer.

---

## 16. Suggested build order

A phased approach so each layer is testable before the next is built.

### Phase 1: Backend interfaces + in-memory implementations
Define the four interfaces. Build in-memory implementations and the smoke-test suite. Nothing above this layer exists yet.

### Phase 2: Job, handler, worker pool (in-process only)
Job model, handler registry, worker pool, job handle, basic retry. In-memory queue only. Test with toy handlers (e.g., a `hello_world` job that just returns a string).

### Phase 3: Persistent backends
Implement SQLite (StateStore, Archive, Queue) and Redis (StateStore, EventLog, Queue). Pass the smoke-test suite.

### Phase 4: Lifecycle and reliability
Visibility timeouts, dead-letter, scheduled jobs, cooperative pause, cancellation. Worker lifecycle events on the EventLog.

### Phase 5: Persistence services
Event flusher, state snapshotter. Verified with worker lifecycle events first.

### Phase 6: Distributed mode
Out-of-process worker pools, separate persister process, CLI commands. Verify the same handler code from Phase 2 works without modification.

### Phase 7: Operability
`skrift workers queues list`, `skrift workers jobs inspect`, `skrift workers dlq`, queue depth metrics, config validation messages.

Each phase should ship with tests that run against all configured backends. Skipping the smoke-test discipline in Phase 1 will hurt later.

---

## Out of scope for this spec

- Agents, sessions, Pydantic AI integration — covered in the separate Agents spec, which builds on this subsystem.
- Specific Redis commands, SQL schemas, or wire formats — implementation detail for backends.
- Authentication, authorization, secret management — assumed to be supplied by host applications.
- Observability/metrics integration beyond emitting events — events are emitted; the host application chooses where they go.
