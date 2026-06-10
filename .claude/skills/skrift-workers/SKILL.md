---
name: skrift-workers
description: "Skrift background workers — job handlers, payload models, queues, retry/dead-letter, execution modes and backends, and operator CLI."
---

# Skrift Workers

Background job system: typed Pydantic payloads, awaitable job handles, retries with dead-lettering, pause/resume, and pluggable memory/SQLAlchemy/Redis backends.

## Defining a Handler

```python
from skrift import handler, Job

class SendWelcomeEmail(Job):  # Job is a Pydantic BaseModel
    user_id: str
    email: str

@handler("send_welcome_email")
async def send_welcome_email(job: SendWelcomeEmail) -> dict:
    await email_backend.send(job.email, "Welcome!")
    return {"sent_to": job.email}  # becomes the job result
```

The payload model is **inferred from the first parameter annotation** (any Pydantic `BaseModel` works; `skrift.Job` is the conventional base). Pass `payload_model=` only when the annotation can't be resolved.

Handler options:

```python
@handler("resize_image", queue="media", max_attempts=5, visibility_timeout=120.0)
async def resize_image(job: ResizeImage, context) -> dict:
    # optional second arg: WorkerContext (runtime, job envelope, paused_state, emit())
    await context.emit(f"media:job:{context.job.id}", {"step": "resizing"})
    ...
```

Registration happens **at import time** — duplicate registration of the same job type raises `ValueError`.

### Handler Discovery — `workers.imports`

Worker processes import your `controllers` modules from app.yaml plus anything under `workers.imports`. Handlers defined outside controller modules must be listed there or they won't exist in the worker:

```yaml
workers:
  imports:
    - myapp.jobs
```

## Submitting Jobs

```python
from skrift import submit

# By payload model (job type resolved from registered model)
handle = await submit(SendWelcomeEmail(user_id="u1", email="a@b.c"))

# By job type string + payload (model or dict — dict is validated)
handle = await submit("send_welcome_email", {"user_id": "u1", "email": "a@b.c"})

# Options
handle = await submit(job, queue="media", scheduled_for=run_at,
                      correlation_id="req-123", job_id="idempotent-key")
```

`job_id=` gives idempotent submission: resubmitting the same id with an identical payload returns the existing job's handle; a different payload raises `JobIdConflict`. Unknown job types raise `KeyError`; payloads that fail validation are dead-lettered as **poison** (the handle resolves to `JobFailed`).

### JobHandle

```python
from skrift import JobFailed, JobCancelled, get_handle

result = await handle                      # awaitable — same as handle.result()
result = await handle.result(timeout=30)   # raises JobFailed / JobCancelled / TimeoutError
state = await handle.status()              # JobState: status, attempt, result, last_error
ok = await handle.cancel()                 # True only while still SUBMITTED (unclaimed)

handle = get_handle(job_id)                # rebuild a handle from a stored job id
```

`JobStatus`: `submitted → claimed → running → completed | failed | dead_lettered | cancelled | paused`.

## Retry & Dead-Letter

```python
from skrift import RetryPolicy

RetryPolicy(max_attempts=3, backoff_seconds=0.0, jitter_seconds=0.0)
# delay = backoff_seconds * (attempt - 1) + uniform(0, jitter_seconds)  — linear backoff
```

Set per handler (`retry_policy=` or shorthand `max_attempts=`) or per submission (`submit(..., retry_policy=...)`). A raised exception retries until attempts are exhausted, then the job dead-letters and awaiting the handle raises `JobFailed`. Raise `PermanentFailure` (from `skrift.workers`) to skip remaining retries and dead-letter immediately.

### Dead Callback — `@handler.on_dead`

```python
from skrift.workers import DeadJobEntry

@handler("sync_crm", max_attempts=3)
async def sync_crm(job: SyncCrm):
    ...

@sync_crm.on_dead
async def alert_ops(entry: DeadJobEntry):
    # entry.job_type, entry.latest_error, entry.cause, entry.attempts (full tracebacks)
    await notify_broadcast("job_dead", job_type=entry.job_type, error=entry.latest_error)
```

DLQ causes: `retries_exhausted`, `permanent_failure` (raised `PermanentFailure`), `reclaim_loop` (claim expired `max_reclaims` times), `poison` (payload validation failed). Replay/discard programmatically via `runtime.retry_dlq_entry(entry_id, force=...)` / `runtime.discard_dlq_entry(entry_id, reason=...)` — `permanent_failure` and `poison` require `force=True`.

## Pause & Resume

Return `Pause` from a handler to cooperatively re-enqueue without consuming a retry attempt. State round-trips through `context.paused_state`:

```python
from skrift import Pause, wake

@handler("long_import")
async def long_import(job: ImportJob, context):
    done = int(context.paused_state.get("done", 0))
    for i in range(done, job.total):
        await process_chunk(i)
        if should_yield():
            return Pause(resume_at=utcnow() + timedelta(seconds=30), state={"done": i + 1})
    return {"imported": job.total}

# Pause(state=...) with no resume_at parks the job until woken explicitly:
await wake(job_id)                      # or wake(job_id, resume_at=...)
```

## Configuration

Enable in app.yaml — the app factory calls `configure_workers()` at startup and starts the in-process pool (unless `out_of_process`):

```yaml
workers:
  enabled: true
  preset: single_node      # custom | local | single_node | distributed
  queues: [default, media]
  concurrency: 4
  imports:
    - myapp.jobs
```

### Presets & Execution Modes

| Preset | Execution | Backends |
|--------|-----------|----------|
| `local` | `inline` — runs in the submitting coroutine | All in-memory |
| `single_node` | `in_process` — background tasks in the web process | All SQLAlchemy (shared DB) |
| `distributed` | `out_of_process` — web submits; `skrift workers run` drains | Redis state/events/queue + SQLAlchemy DLQ/archive |
| `custom` (default) | whatever `execution` says (default `inline`) | whatever `backends` says (default memory) |

Preset fields are overridable individually:

```yaml
workers:
  preset: distributed
  backends:
    queue: skrift.workers.sqlalchemy:SQLAlchemyQueue   # module:ClassName import string
```

### Backend Validity

- **Memory** backends are process-local: fine for `inline`/`in_process` in one process, **rejected** for `out_of_process` web, standalone workers, the persister, and inspection CLI (override locally with `--allow-memory-backends`).
- **SQLAlchemy** backends share state via the configured database — good single-node default.
- **Redis** backends (state store, event log, queue) read `redis.url`/`redis.prefix` (or `SKRIFT_WORKERS_REDIS_URL`); there is no Redis DLQ/archive, so the distributed preset keeps those on SQLAlchemy.

For standalone scripts/tests, configure manually instead of via app.yaml:

```python
from skrift import configure_workers, local_executor

runtime = configure_workers(mode="inline")                      # tests: synchronous execution
async with local_executor(mode="in_process", concurrency=2) as runtime:
    handle = await runtime.submit(MyJob(...))                   # temporary runtime, auto-stopped
```

## CLI

```bash
skrift workers run --queue media --concurrency 8 --import myapp.jobs  # standalone worker process
skrift workers persister              # event flush + state snapshots + retention pruning (--once for one pass)
skrift workers prune --json           # one retention pruning pass
skrift workers queues list            # queue depth / oldest-ready age
skrift workers jobs inspect JOB_ID    # job state + lifecycle events
skrift workers dlq list               # filters: --queue --job-type --cause --state ...
skrift workers dlq inspect ENTRY_ID
skrift workers dlq retry ENTRY_ID     # --force for permanent_failure/poison; omit IDs to use filters
skrift workers dlq discard ENTRY_ID --reason "triaged"
skrift workers dlq export             # JSON export
```

The admin dashboard also exposes a live workers view (`/admin/workers`) backed by `runtime.inspect()`.

## Key Files

| File | Purpose |
|------|---------|
| `skrift/workers/registry.py` | `@handler` decorator, `HandlerRegistry`, `registry` singleton |
| `skrift/workers/runtime.py` | `WorkerRuntime`, `submit`, `JobHandle`, `configure_workers`, `local_executor`, `wake` |
| `skrift/workers/models.py` | `Job`, `JobEnvelope`, `JobState`, `RetryPolicy`, `Pause`, `DeadJobEntry` |
| `skrift/workers/memory.py` / `sqlalchemy.py` / `redis.py` | Backend implementations |
| `skrift/workers/persistence.py` | `EventFlusher`, `StateSnapshotter`, `WorkerPruner` |
| `skrift/config.py` | `WorkersConfig` (presets), `WorkerBackendConfig`, persistence/retention configs |
| `skrift/cli.py` | `skrift workers ...` command group |
| `docs/reference/workers.md` | Full config/backend-contract reference |
| `demo/worker-demo/` | Runnable demo with pause/fail/multi-queue jobs |
