# Workers

<span class="skill-badge advanced">:material-star::material-star::material-star: Advanced</span>

Skrift workers run typed background jobs from the same application code that defines your controllers, hooks, and models. Use them for work that should not block an HTTP request: image processing, webhook delivery, indexing, imports, email fanout, or any task that benefits from retries and operator visibility.

## How Workers Fit Together

A worker job has four moving parts:

| Part | Purpose |
|------|---------|
| **Job payload** | A Pydantic model describing the work input |
| **Handler** | An async or sync function registered for one job type |
| **Runtime** | The process-local coordinator used by web, worker, and admin code |
| **Backends** | Storage for state, lifecycle events, queues, DLQ entries, and archives |

The same public API works in every execution mode. The configured runtime decides whether a submitted job runs immediately, in an in-process worker pool, or in a separate worker process.

## Configuration

Enable workers in `app.yaml`:

```yaml
workers:
  enabled: true
  preset: single_node
  queues:
    - default
    - slow
  concurrency: 4
  imports:
    - myapp.jobs
```

Choose one of the built-in presets:

| Preset | Execution | Backends | Best for |
|--------|-----------|----------|----------|
| `local` | `inline` | In-memory | Tests and very small local demos |
| `single_node` | `in_process` | SQLAlchemy | One web process or one host sharing the app database |
| `distributed` | `out_of_process` | Redis hot path + SQLAlchemy archive/DLQ | Separate web, worker, and persister processes |
| `custom` | Whatever you configure | Whatever you configure | Advanced deployments and custom backend mixes |

For production-style deployments, avoid memory backends outside a single process. Memory queues and state stores are not shared between web and worker processes.

See [Workers Reference](../reference/workers.md) for the full configuration schema and backend matrix.

## Creating Jobs

Define a payload by subclassing `skrift.Job`, then register a handler with `@skrift.handler`.

```python
import skrift


class ResizeImage(skrift.Job):
    asset_id: str
    width: int
    height: int


@skrift.handler("media.resize_image", queue="images", max_attempts=3)
async def resize_image(job: ResizeImage) -> dict:
    # Load the asset, resize it, and save the variants.
    return {"asset_id": job.asset_id, "status": "resized"}
```

Handler defaults can set the queue, maximum attempts, visibility timeout, or a full retry policy:

```python
@skrift.handler(
    "media.resize_image",
    queue="images",
    retry_policy=skrift.RetryPolicy(max_attempts=5, backoff_seconds=30, jitter_seconds=5),
    visibility_timeout=120,
)
async def resize_image(job: ResizeImage) -> dict:
    return {"asset_id": job.asset_id, "status": "resized"}
```

Handlers are registered when their module is imported. Add the module to `workers.imports` if it is not already imported by a controller or hook:

```yaml
workers:
  enabled: true
  imports:
    - myapp.jobs
```

### Handler Context

If a handler accepts a second argument, Skrift passes a `WorkerContext`.

```python
@skrift.handler("reports.generate", queue="reports", max_attempts=2)
async def generate_report(job: GenerateReport, context) -> dict:
    await context.emit(
        f"reports:{context.job.id}",
        {"message": "started", "report_id": job.report_id},
    )
    return {"report_id": job.report_id}
```

The context exposes:

| Attribute | Description |
|-----------|-------------|
| `context.runtime` | The active `WorkerRuntime` |
| `context.job` | The current `JobEnvelope` metadata |
| `context.paused_state` | Empty unless this attempt is resuming from a previous `skrift.Pause` |
| `context.emit(stream, event)` | Append a JSON-serializable custom event to the configured event log |

Use application-owned stream names for custom events, such as `reports:{job_id}` or `media:resize`. Avoid the `workers:` prefix unless you are intentionally writing framework-level worker events.

## Submitting Jobs

Submit by passing the typed payload when you have the job model in Python. This gives you editor/type-checker help and lets Skrift infer the registered job type.

```python
handle = await skrift.submit(
    ResizeImage(asset_id="asset_123", width=1200, height=800)
)

result = await handle.result(timeout=30)
```

Submit by job type and payload when the job type is data-driven, such as an API request, admin tool, or replay/import path.

```python
handle = await skrift.submit(
    "media.resize_image",
    {"asset_id": "asset_123", "width": 1200, "height": 800},
)
```

Common submission options:

```python
from datetime import datetime, timedelta, timezone

handle = await skrift.submit(
    ResizeImage(asset_id="asset_123", width=1200, height=800),
    queue="images",
    scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=10),
    retry_policy=skrift.RetryPolicy(max_attempts=5, backoff_seconds=30, jitter_seconds=5),
    correlation_id="request-abc",
)
```

| Option | Description |
|--------|-------------|
| `queue` | Override the handler's default queue |
| `scheduled_for` | Delay execution until a UTC datetime |
| `retry_policy` | Override max attempts, backoff, and jitter |
| `correlation_id` | Store application metadata such as a trace or request id |
| `parent_job_id` | Store lineage metadata linking this job to another job |
| `visibility_timeout` | Override the claim timeout for this job |

`correlation_id` and `parent_job_id` are metadata fields. Skrift stores them in job state, but they do not create cascading cancellation, lineage traversal, or admin filtering by themselves.

## Managing Jobs

`submit()` returns a `JobHandle`.

```python
handle = await skrift.submit(ResizeImage(asset_id="asset_123", width=1200, height=800))

state = await handle.status()
cancelled = await handle.cancel()
result = await handle.result(timeout=60)
```

Reconstruct a handle when you already have a job id:

```python
handle = skrift.get_handle(job_id)
state = await handle.status()
```

Wake a paused or delayed job:

```python
await skrift.wake(job_id)
```

`cancel()` cancels jobs that are still submitted and have not been claimed. It returns `False` for running, completed, failed, dead-lettered, cancelled, or paused jobs. Current handlers do not receive a cancellation signal, so long-running handlers must still be written to finish safely and idempotently.

`handle.result()` returns the handler result for completed jobs. It raises:

| Exception | When |
|-----------|------|
| `skrift.JobFailed` | The job failed or was dead-lettered |
| `skrift.JobCancelled` | The job was cancelled |
| `asyncio.TimeoutError` | The optional timeout expired |
| `KeyError` | The job id is unknown to the current runtime state store |

## Retries, Pauses, And DLQ

Unhandled exceptions emit a failure lifecycle event. If attempts remain, the job is requeued with the configured backoff. Once attempts are exhausted, Skrift moves the job to the dead-letter queue.

Handlers should be idempotent. A job can run more than once if a handler raises and is retried, if a worker crashes after doing external work but before acking the queue claim, or if a visibility timeout expires and another worker reclaims the job. Use durable idempotency keys, check whether external side effects already happened, and set `visibility_timeout` high enough for the expected work.

Raise `PermanentFailure` from `skrift.workers` to skip remaining retries:

```python
from skrift.workers import PermanentFailure


@skrift.handler("webhook.deliver", queue="webhooks", max_attempts=5)
async def deliver_webhook(job: DeliverWebhook) -> None:
    response = await send_webhook(job)
    if response.status_code == 410:
        raise PermanentFailure("endpoint is gone")
```

Return `skrift.Pause` for cooperative long-running workflows:

```python
from datetime import datetime, timedelta, timezone


@skrift.handler("import.contacts", queue="imports")
async def import_contacts(job: ImportContacts, context) -> skrift.Pause | dict:
    next_page = await import_one_page(job, context.paused_state.get("page", 1))
    if next_page is not None:
        return skrift.Pause(
            resume_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            state={"page": next_page},
        )
    return {"status": "complete"}
```

Paused jobs keep their job state. If `resume_at` is set, the queue makes the job eligible again at that time. If `resume_at` is omitted, the job stays paused until external code calls `wake(job_id)`.

Use manual pauses when an operator or webhook must unblock a workflow:

```python
@skrift.handler("review.wait_for_approval", queue="reviews")
async def wait_for_approval(job: ReviewJob, context) -> skrift.Pause | dict:
    if not await is_approved(job.review_id):
        return skrift.Pause(state={"waiting": True})
    return {"approved": True}
```

### Dead-Letter Causes And States

Dead-letter entries keep structured cause and state fields so operators can filter and batch actions.

| Cause | Meaning |
|-------|---------|
| `retries_exhausted` | The handler kept failing until `max_attempts` was reached |
| `permanent_failure` | The handler raised `PermanentFailure` |
| `reclaim_loop` | A job was claimed and timed out too many times |
| `poison` | A submitted payload could not be validated for its handler |

| State | Meaning |
|-------|---------|
| `open` | Needs operator attention |
| `replayed` | Was retried as a new job |
| `discarded` | Was marked resolved without retrying |

Discarding a DLQ entry does not delete forensic data. It cancels the queue marker if present, updates the entry state to `discarded`, and stores the optional reason.

## Running Workers

For `inline` and `in_process` modes, the web app configures the runtime at startup. For `out_of_process`, the web process only submits jobs; separate worker processes drain queues.

```bash
skrift workers run --queue default --queue slow --concurrency 4
```

Run the persistence service to copy hot-path lifecycle events and state snapshots into the archive:

```bash
skrift workers persister
```

Run one persistence and retention pass, useful in cron-style environments:

```bash
skrift workers persister --once
skrift workers prune --json
```

`persister --once` flushes lifecycle events, snapshots configured state, and then runs pruning. `prune` runs only the retention pass.

Local CLI experiments with memory backends require an explicit opt-in:

```bash
skrift workers queues list --allow-memory-backends
```

## Inspecting And Operating

The admin worker pages expose queue depth, recent jobs, lifecycle events, registered handlers, queue wait history, completed job history, and DLQ entries.

Common CLI workflows:

```bash
skrift workers queues list
skrift workers jobs inspect JOB_ID --json
skrift workers dlq list
skrift workers dlq inspect ENTRY_ID --json
```

DLQ retry creates a new job with clean retry state. You can operate on explicit IDs or filter a batch:

```bash
skrift workers dlq retry ENTRY_ID
skrift workers dlq retry --queue webhooks --cause retries_exhausted --since 1h
skrift workers dlq discard --job-type media.resize_image --reason "bad deploy"
skrift workers dlq retry --cause permanent_failure --dry-run
```

Filter-based retry and discard default to `state=open` so previously replayed or discarded history is not changed accidentally. Permanent failures and poison payloads require `--force`:

```bash
skrift workers dlq retry ENTRY_ID --force
skrift workers dlq retry --cause poison --force
```

The canonical command list and option reference lives in [CLI Reference](../reference/cli.md#skrift-workers).

## Deployment Patterns

### Single process or small single-node app

```yaml
workers:
  enabled: true
  preset: single_node
  queues: [default, slow]
  concurrency: 4
```

This runs an in-process worker pool and stores worker data in your configured SQLAlchemy database.

### Separate worker processes

```yaml
redis:
  url: $REDIS_URL

workers:
  enabled: true
  preset: distributed
  queues: [default, slow]
  concurrency: 4
  imports:
    - myapp.jobs
```

Then run at least:

```bash
skrift serve --host 0.0.0.0
skrift workers run --queue default --queue slow
skrift workers persister
```

The distributed preset uses Redis for state, lifecycle events, and queue claims, with SQLAlchemy for archived events, snapshots, and DLQ records.

You can run multiple worker processes against the same queues to scale out:

```bash
skrift workers run --queue default --queue slow --concurrency 4
skrift workers run --queue default --queue slow --concurrency 4
```

Each process claims jobs from the shared queue backend. Keep handlers idempotent because crashes and visibility-timeout reclaims can cause a job to run more than once.

Configured `workers.imports` are imported at startup. If one of those modules raises during import, the web app or worker command fails fast rather than starting with missing handlers.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Jobs submit but never run | Worker process is running, queues match, and handler modules are imported |
| Standalone worker cannot find handlers | Add the module to `workers.imports` or pass `--import myapp.jobs` |
| CLI refuses memory backends | Use a shared backend preset or pass `--allow-memory-backends` for local tests |
| Jobs repeat after worker crash | Increase `visibility_timeout` for long-running handlers or make handlers idempotent |
| Jobs end up in DLQ quickly | Check `max_attempts`, handler exceptions, poison payloads, and `PermanentFailure` usage |
| Redis hot-path data grows | Run `skrift workers persister` and keep `workers.retention.enabled` true |
