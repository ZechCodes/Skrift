"""Tests for the private-beta worker subsystem."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.db.base import Base
from skrift.workers import (
    LIFECYCLE_STREAM,
    HandlerRegistry,
    InMemoryArchive,
    InMemoryEventLog,
    InMemoryQueue,
    InMemoryStateStore,
    Job,
    DeadJobEntry,
    DeadLetterCause,
    DeadLetterState,
    EventFlusher,
    JobFailed,
    JobStatus,
    PermanentFailure,
    Pause,
    QueueStats,
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    RetryPolicy,
    SQLAlchemyArchive,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
    StateSnapshotter,
    WorkerBackendConfig,
    WorkerConfig,
    WorkerRuntime,
)
from skrift.workers.models import JobEnvelope, JobIdConflict, utcnow
from skrift.workers.registry import registry


@pytest.fixture(autouse=True)
def clean_worker_registry():
    registry.clear()
    yield
    registry.clear()


class Greeting(Job):
    name: str


@pytest.fixture
async def worker_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workers.db'}",
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def fake_redis_client():
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis()
    await client.flushall()
    yield client
    await client.aclose()


async def test_state_store_supports_ttl_prefix_scan_and_atomic_update():
    store = InMemoryStateStore()
    await store.set("jobs:a", 1)
    await store.set("jobs:b", 2)
    await store.set("other:c", 3)

    async def increment(value):
        return (value or 0) + 4

    assert await store.update("jobs:a", increment) == 5
    assert await store.get("jobs:a") == 5
    assert await store.keys("jobs:") == ["jobs:a", "jobs:b"]

    await store.set("jobs:short", "gone", ttl=0.01)
    await asyncio.sleep(0.02)
    assert await store.get("jobs:short") is None


async def test_redis_state_store_supports_ttl_prefix_scan_and_atomic_update(fake_redis_client):
    store = RedisStateStore(client=fake_redis_client, prefix="test:workers")
    await store.set("jobs:a", 1)
    await store.set("jobs:b", 2)
    await store.set("other:c", 3)

    async def increment(value):
        return (value or 0) + 4

    assert await store.update("jobs:a", increment) == 5
    assert await store.get("jobs:a") == 5
    assert await store.keys("jobs:") == ["jobs:a", "jobs:b"]

    await store.set("jobs:short", "gone", ttl=0.01)
    await asyncio.sleep(0.02)
    assert await store.get("jobs:short") is None


async def test_event_log_replays_and_live_tails():
    log = InMemoryEventLog()
    assert await log.append("s", {"n": 1}) == 0
    assert await log.append("s", {"n": 2}) == 1
    assert await log.append("s", {"n": 3, "job_id": "job-1"}) == 2
    assert await log.read("s", from_position=0, limit=2) == [
        (0, {"n": 1}),
        (1, {"n": 2}),
    ]
    assert await log.read_filtered("s", filters={"job_id": "job-1"}) == [
        (2, {"n": 3, "job_id": "job-1"})
    ]

    subscription = log.subscribe("s", from_position=3)
    next_event = asyncio.create_task(anext(subscription))
    await log.append("s", {"n": 4})
    assert await asyncio.wait_for(next_event, timeout=1) == (3, {"n": 4})
    await subscription.aclose()


async def test_redis_event_log_replays_filters_tails_and_live_tails(fake_redis_client):
    log = RedisEventLog(client=fake_redis_client, prefix="test:workers")
    assert await log.append("s", {"n": 1}) == 0
    assert await log.append("s", {"n": 2}) == 1
    assert await log.append("s", {"n": 3, "job_id": "job-1"}) == 2
    assert await log.read("s", from_position=0, limit=2) == [
        (0, {"n": 1}),
        (1, {"n": 2}),
    ]
    assert await log.read_filtered("s", filters={"job_id": "job-1"}) == [
        (2, {"n": 3, "job_id": "job-1"})
    ]
    assert await log.read_tail("s", limit=2) == [
        (1, {"n": 2}),
        (2, {"n": 3, "job_id": "job-1"}),
    ]

    subscription = log.subscribe("s", from_position=3)
    next_event = asyncio.create_task(anext(subscription))
    await log.append("s", {"n": 4})
    assert await asyncio.wait_for(next_event, timeout=1) == (3, {"n": 4})
    await subscription.aclose()


async def test_queue_claim_ack_delayed_visibility_reclaim_and_dead_letter():
    queue = InMemoryQueue()
    delayed = JobEnvelope(type="delayed", scheduled_for=utcnow() + timedelta(seconds=0.2))
    await queue.submit(delayed)
    assert await queue.claim(["default"], visibility_timeout=0.01) is None
    await asyncio.sleep(0.22)

    claimed = await queue.claim(["default"], visibility_timeout=0.01)
    assert claimed is not None
    assert claimed.job.id == delayed.id
    await asyncio.sleep(0.02)
    reclaimed = await queue.claim(["default"], visibility_timeout=1)
    assert reclaimed is not None
    assert reclaimed.job.id == delayed.id
    await queue.nack("default", delayed.id, reclaimed.token, dead_letter=True)
    stats = await queue.stats("default")
    assert stats.dead_lettered == 1

    ready = JobEnvelope(type="ready")
    await queue.submit(ready)
    claimed_ready = await queue.claim(["default"], visibility_timeout=1)
    assert claimed_ready is not None
    await queue.ack("default", ready.id, claimed_ready.token)
    stats = await queue.stats("default")
    assert stats.ready == 0

    old_ready = JobEnvelope(type="old_ready", scheduled_for=utcnow() - timedelta(seconds=12))
    await queue.submit(old_ready)
    stats = await queue.stats("default")
    assert stats.ready == 1
    assert old_ready.ready_since == old_ready.scheduled_for
    assert stats.oldest_ready_age_seconds >= 12


async def test_redis_queue_claim_ack_delayed_visibility_reclaim_and_dead_letter(
    fake_redis_client,
):
    queue = RedisQueue(client=fake_redis_client, prefix="test:workers")
    delayed = JobEnvelope(type="delayed", scheduled_for=utcnow() + timedelta(seconds=0.2))
    await queue.submit(delayed)
    assert await queue.claim(["default"], visibility_timeout=0.01) is None
    await asyncio.sleep(0.22)

    claimed = await queue.claim(["default"], visibility_timeout=0.01)
    assert claimed is not None
    assert claimed.job.id == delayed.id
    await asyncio.sleep(0.02)
    reclaimed = await queue.claim(["default"], visibility_timeout=1)
    assert reclaimed is not None
    assert reclaimed.job.id == delayed.id
    await queue.nack("default", delayed.id, reclaimed.token, dead_letter=True)
    stats = await queue.stats("default")
    assert stats.dead_lettered == 1

    ready = JobEnvelope(type="ready")
    await queue.submit(ready)
    claimed_ready = await queue.claim(["default"], visibility_timeout=1)
    assert claimed_ready is not None
    await queue.ack("default", ready.id, claimed_ready.token)
    stats = await queue.stats("default")
    assert stats.ready == 0

    old_ready = JobEnvelope(type="old_ready", scheduled_for=utcnow() - timedelta(seconds=12))
    await queue.submit(old_ready)
    stats = await queue.stats("default")
    assert stats.ready == 1
    assert old_ready.ready_since == old_ready.scheduled_for
    assert stats.oldest_ready_age_seconds >= 12


async def test_redis_queue_cancel_and_wake(fake_redis_client):
    queue = RedisQueue(client=fake_redis_client, prefix="test:workers")
    cancellable = JobEnvelope(type="cancel")
    await queue.submit(cancellable)
    assert await queue.cancel("default", cancellable.id) is True
    assert await queue.cancel("default", cancellable.id) is False

    paused = JobEnvelope(type="pause", scheduled_for=utcnow() + timedelta(hours=1))
    await queue.submit(paused)
    assert (await queue.stats("default")).delayed == 1
    assert await queue.wake("default", paused.id) is True
    assert (await queue.stats("default")).ready == 1


async def test_archive_stores_events_and_state_history():
    archive = InMemoryArchive()
    await archive.bulk_insert_events([("s", 0, {"a": 1}), ("s", 1, {"a": 2})])
    await archive.upsert_state_snapshot("job:1", {"state": "one"})
    await archive.upsert_state_snapshot("job:1", {"state": "two"})

    assert await archive.query_events("s", from_position=1) == [(1, {"a": 2})]
    assert await archive.latest_state_snapshot("job:1") == {"state": "two"}
    assert len(await archive.historical_state_snapshots("job:1")) == 2


async def test_event_flusher_archives_events_and_tracks_cursor():
    event_log = InMemoryEventLog()
    archive = InMemoryArchive()
    state_store = InMemoryStateStore()
    await event_log.append("demo", {"n": 1})
    await event_log.append("demo", {"n": 2})

    flusher = EventFlusher(
        event_log=event_log,
        archive=archive,
        state_store=state_store,
        streams=("demo",),
        batch_size=1,
    )

    assert await flusher.flush_once("demo") == 1
    assert await archive.query_events("demo") == [(0, {"n": 1})]
    assert await state_store.get("workers:persister:event_cursors:demo") == 1

    await event_log.append("demo", {"n": 3})
    assert await flusher.flush_once() == 1
    assert await flusher.flush_once() == 1
    assert await archive.query_events("demo") == [
        (0, {"n": 1}),
        (1, {"n": 2}),
        (2, {"n": 3}),
    ]
    assert await flusher.flush_once() == 0


async def test_state_snapshotter_archives_configured_keys_and_prefixes():
    state_store = InMemoryStateStore()
    archive = InMemoryArchive()
    await state_store.set("workers:jobs:a", {"status": "ready"})
    await state_store.set("workers:jobs:b", {"status": "running"})
    await state_store.set("workers:runtime", {"queues": 2})
    await state_store.set("other", {"ignored": True})

    snapshotter = StateSnapshotter(
        state_store=state_store,
        archive=archive,
        keys=("workers:runtime",),
        prefixes=("workers:jobs:",),
    )

    assert await snapshotter.snapshot_once() == 3
    assert await archive.latest_state_snapshot("workers:runtime") == {"queues": 2}
    assert await archive.latest_state_snapshot("workers:jobs:a") == {"status": "ready"}
    assert await archive.latest_state_snapshot("other") is None


async def test_sqlalchemy_state_store_supports_ttl_prefix_scan_and_atomic_update(
    worker_session_maker,
):
    store = SQLAlchemyStateStore(session_maker=worker_session_maker)
    await store.set("jobs:a", 1)
    await store.set("jobs:b", 2)
    await store.set("other:c", 3)

    async def increment(value):
        return (value or 0) + 4

    assert await store.update("jobs:a", increment) == 5
    assert await store.get("jobs:a") == 5
    assert await store.keys("jobs:") == ["jobs:a", "jobs:b"]

    await store.set("jobs:short", "gone", ttl=0.01)
    await asyncio.sleep(0.02)
    assert await store.get("jobs:short") is None


async def test_sqlalchemy_event_log_replays_and_live_tails(worker_session_maker):
    log = SQLAlchemyEventLog(session_maker=worker_session_maker)
    assert await log.append("s", {"n": 1}) == 0
    assert await log.append("s", {"n": 2}) == 1
    assert await log.append("s", {"n": 3, "job_id": "job-1"}) == 2
    assert await log.read("s", from_position=0, limit=2) == [
        (0, {"n": 1}),
        (1, {"n": 2}),
    ]
    assert await log.read_filtered("s", filters={"job_id": "job-1"}) == [
        (2, {"n": 3, "job_id": "job-1"})
    ]

    subscription = log.subscribe("s", from_position=3)
    next_event = asyncio.create_task(anext(subscription))
    await log.append("s", {"n": 4})
    assert await asyncio.wait_for(next_event, timeout=1) == (3, {"n": 4})
    await subscription.aclose()


async def test_sqlalchemy_queue_claim_ack_delayed_visibility_reclaim_and_dead_letter(
    worker_session_maker,
):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    delayed = JobEnvelope(type="delayed", scheduled_for=utcnow() + timedelta(seconds=0.2))
    await queue.submit(delayed)
    assert await queue.claim(["default"], visibility_timeout=0.01) is None
    await asyncio.sleep(0.22)

    claimed = await queue.claim(["default"], visibility_timeout=0.01)
    assert claimed is not None
    assert claimed.job.id == delayed.id
    await asyncio.sleep(0.02)
    reclaimed = await queue.claim(["default"], visibility_timeout=1)
    assert reclaimed is not None
    assert reclaimed.job.id == delayed.id
    assert reclaimed.job.reclaim_count == 1
    await queue.nack("default", delayed.id, reclaimed.token, dead_letter=True)
    stats = await queue.stats("default")
    assert stats.dead_lettered == 1

    ready = JobEnvelope(type="ready")
    await queue.submit(ready)
    claimed_ready = await queue.claim(["default"], visibility_timeout=1)
    assert claimed_ready is not None
    await queue.ack("default", ready.id, claimed_ready.token)
    stats = await queue.stats("default")
    assert stats.ready == 0

    old_ready = JobEnvelope(type="old_ready", scheduled_for=utcnow() - timedelta(seconds=12))
    await queue.submit(old_ready)
    stats = await queue.stats("default")
    assert stats.ready == 1
    assert stats.oldest_ready_age_seconds >= 12


async def test_sqlalchemy_queue_concurrent_claims_are_atomic(worker_session_maker):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="only_once")
    await queue.submit(job)

    claims = await asyncio.gather(
        *[
            queue.claim(["default"], visibility_timeout=5)
            for _ in range(8)
        ]
    )

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].job.id == job.id


async def test_sqlalchemy_archive_stores_events_and_state_history(worker_session_maker):
    archive = SQLAlchemyArchive(session_maker=worker_session_maker)
    await archive.bulk_insert_events([("s", 0, {"a": 1}), ("s", 1, {"a": 2})])
    await archive.upsert_state_snapshot("job:1", {"state": "one"})
    await archive.upsert_state_snapshot("job:1", {"state": "two"})

    assert await archive.query_events("s", from_position=1) == [(1, {"a": 2})]
    assert await archive.latest_state_snapshot("job:1") == {"state": "two"}
    assert len(await archive.historical_state_snapshots("job:1")) == 2


async def test_sqlalchemy_persistence_services_archive_hot_path_data(worker_session_maker):
    event_log = SQLAlchemyEventLog(session_maker=worker_session_maker)
    state_store = SQLAlchemyStateStore(session_maker=worker_session_maker)
    archive = SQLAlchemyArchive(session_maker=worker_session_maker)
    await event_log.append("demo", {"n": 1})
    await event_log.append("demo", {"n": 2})
    await state_store.set("workers:jobs:a", {"status": "ready"})

    flusher = EventFlusher(
        event_log=event_log,
        archive=archive,
        state_store=state_store,
        streams=("demo",),
        batch_size=10,
    )
    snapshotter = StateSnapshotter(
        state_store=state_store,
        archive=archive,
        prefixes=("workers:jobs:",),
    )

    assert await flusher.flush_once() == 2
    assert await archive.query_events("demo") == [(0, {"n": 1}), (1, {"n": 2})]
    assert await state_store.get("workers:persister:event_cursors:demo") == 2
    assert await snapshotter.snapshot_once() == 1
    assert await archive.latest_state_snapshot("workers:jobs:a") == {"status": "ready"}


async def test_sqlalchemy_dead_letter_store_filters_and_saves(worker_session_maker):
    store = SQLAlchemyDeadLetterStore(session_maker=worker_session_maker)
    job = JobEnvelope(type="broken", queue="critical")
    entry = DeadJobEntry(
        job=job,
        queue=job.queue,
        job_type=job.type,
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
        latest_error="boom",
    )
    await store.create(entry)

    listed = await store.list(queue="critical", job_type="broken")
    assert [item.id for item in listed] == [entry.id]
    entry.state = DeadLetterState.DISCARDED
    await store.save(entry)
    assert (await store.get(entry.id)).state == DeadLetterState.DISCARDED
    assert await store.list(state=DeadLetterState.OPEN.value) == []


async def test_inline_submit_returns_result_and_lifecycle_events():
    @skrift.handler("greet")
    async def greet(job: Greeting):
        return f"hello {job.name}"

    runtime = skrift.configure_workers(mode="inline")
    handle = await runtime.submit(Greeting(name="Ada"))

    assert await handle.result() == "hello Ada"
    assert await runtime.handle(handle.id).result() == "hello Ada"
    events = await runtime.event_log.read(LIFECYCLE_STREAM)
    assert [event["type"] for _, event in events] == [
        "job_submitted",
        "job_claimed",
        "job_started",
        "job_completed",
    ]


async def test_out_of_process_submit_only_enqueues_job():
    calls = []

    @skrift.handler("greet")
    async def greet(job: Greeting):
        calls.append(job.name)
        return f"hello {job.name}"

    runtime = skrift.configure_workers(mode="out_of_process")
    handle = await runtime.submit(Greeting(name="Ada"))

    assert calls == []
    state = await handle.status()
    assert state.status == JobStatus.SUBMITTED
    events = await runtime.event_log.read(LIFECYCLE_STREAM)
    assert [event["type"] for _, event in events] == ["job_submitted"]
    claimed = await runtime.queue.claim(["default"], visibility_timeout=1)
    assert claimed is not None
    assert claimed.job.id == handle.id


def test_configure_workers_instantiates_import_path_backends():
    runtime = skrift.configure_workers(
        mode="inline",
        backend_imports=WorkerBackendConfig(
            state_store="skrift.workers.memory:InMemoryStateStore",
            event_log="skrift.workers.memory:InMemoryEventLog",
            queue="skrift.workers.memory:InMemoryQueue",
            dead_letter_store="skrift.workers.memory:InMemoryDeadLetterStore",
            archive="skrift.workers.memory:InMemoryArchive",
        ),
    )

    assert isinstance(runtime.state_store, InMemoryStateStore)
    assert isinstance(runtime.event_log, InMemoryEventLog)
    assert isinstance(runtime.queue, InMemoryQueue)


def test_configure_workers_rejects_backend_with_wrong_interface():
    with pytest.raises(TypeError, match="does not implement queue"):
        skrift.configure_workers(
            mode="inline",
            backend_imports={
                "queue": "skrift.workers.memory:InMemoryStateStore",
            },
        )


def test_workers_config_uses_import_paths():
    from skrift.config import WorkersConfig

    config = WorkersConfig(
        enabled=True,
        execution="in_process",
        queues=["emails"],
        concurrency=3,
        backends={
            "state_store": "myapp.workers:SQLAlchemyStateStore",
            "event_log": "myapp.workers:SQLAlchemyEventLog",
            "queue": "myapp.workers:SQLAlchemyQueue",
            "dead_letter_store": "myapp.workers:SQLAlchemyDeadLetterStore",
            "archive": "myapp.workers:SQLAlchemyArchive",
        },
    )

    assert config.backends.queue == "myapp.workers:SQLAlchemyQueue"
    assert config.backends.archive == "myapp.workers:SQLAlchemyArchive"
    assert config.queues == ["emails"]


async def test_configure_workers_instantiates_sqlalchemy_import_path_backends(
    worker_session_maker,
):
    runtime = skrift.configure_workers(
        mode="inline",
        backend_imports=WorkerBackendConfig(
            state_store="skrift.workers.sqlalchemy:SQLAlchemyStateStore",
            event_log="skrift.workers.sqlalchemy:SQLAlchemyEventLog",
            queue="skrift.workers.sqlalchemy:SQLAlchemyQueue",
            dead_letter_store="skrift.workers.sqlalchemy:SQLAlchemyDeadLetterStore",
            archive="skrift.workers.sqlalchemy:SQLAlchemyArchive",
        ),
        session_maker=worker_session_maker,
    )

    assert isinstance(runtime.state_store, SQLAlchemyStateStore)
    assert isinstance(runtime.event_log, SQLAlchemyEventLog)
    assert isinstance(runtime.queue, SQLAlchemyQueue)
    assert isinstance(runtime.dead_letter_store, SQLAlchemyDeadLetterStore)
    assert isinstance(runtime.archive, SQLAlchemyArchive)


async def test_sqlalchemy_runtime_persists_completion_and_events(worker_session_maker):
    @skrift.handler("sqlalchemy_greet")
    async def sqlalchemy_greet(job: Greeting):
        return f"hello {job.name}"

    runtime = skrift.configure_workers(
        mode="inline",
        backend_imports=WorkerBackendConfig(
            state_store="skrift.workers.sqlalchemy:SQLAlchemyStateStore",
            event_log="skrift.workers.sqlalchemy:SQLAlchemyEventLog",
            queue="skrift.workers.sqlalchemy:SQLAlchemyQueue",
            dead_letter_store="skrift.workers.sqlalchemy:SQLAlchemyDeadLetterStore",
            archive="skrift.workers.sqlalchemy:SQLAlchemyArchive",
        ),
        session_maker=worker_session_maker,
    )

    handle = await runtime.submit(Greeting(name="Ada"))
    assert await handle.result() == "hello Ada"

    restored = WorkerRuntime(
        state_store=SQLAlchemyStateStore(session_maker=worker_session_maker),
        event_log=SQLAlchemyEventLog(session_maker=worker_session_maker),
        queue=SQLAlchemyQueue(session_maker=worker_session_maker),
        dead_letter_store=SQLAlchemyDeadLetterStore(session_maker=worker_session_maker),
    )
    state = await restored.get_job_state(handle.id)
    assert state is not None
    assert state.status == JobStatus.COMPLETED
    assert state.result == "hello Ada"
    snapshot = await restored.inspect(job_limit=0)
    assert snapshot["jobs_total"] == 1
    assert snapshot["jobs_active_total"] == 0
    assert [event["type"] for _, event in await restored.event_log.read(LIFECYCLE_STREAM)] == [
        "job_submitted",
        "job_claimed",
        "job_started",
        "job_completed",
    ]


async def test_sqlalchemy_runtime_persists_dlq_and_replay(worker_session_maker):
    should_fail = True

    @skrift.handler("sqlalchemy_replay", max_attempts=1)
    async def sqlalchemy_replay(job: Greeting):
        if should_fail:
            raise RuntimeError("first failure")
        return "ok"

    runtime = skrift.configure_workers(
        mode="inline",
        backend_imports=WorkerBackendConfig(
            state_store="skrift.workers.sqlalchemy:SQLAlchemyStateStore",
            event_log="skrift.workers.sqlalchemy:SQLAlchemyEventLog",
            queue="skrift.workers.sqlalchemy:SQLAlchemyQueue",
            dead_letter_store="skrift.workers.sqlalchemy:SQLAlchemyDeadLetterStore",
            archive="skrift.workers.sqlalchemy:SQLAlchemyArchive",
        ),
        session_maker=worker_session_maker,
    )

    handle = await runtime.submit(Greeting(name="Ada"))
    with pytest.raises(JobFailed, match="first failure"):
        await handle.result()
    entry = (await runtime.inspect_dlq())[0]

    restored = WorkerRuntime(
        config=WorkerConfig(mode="inline"),
        state_store=SQLAlchemyStateStore(session_maker=worker_session_maker),
        event_log=SQLAlchemyEventLog(session_maker=worker_session_maker),
        queue=SQLAlchemyQueue(session_maker=worker_session_maker),
        dead_letter_store=SQLAlchemyDeadLetterStore(session_maker=worker_session_maker),
    )
    restored.registry = runtime.registry
    assert (await restored.get_dlq_entry(entry.id)).latest_error == "RuntimeError: first failure"

    should_fail = False
    replay = await restored.retry_dlq_entry(entry.id)
    assert await replay.result() == "ok"
    replayed_entry = await restored.get_dlq_entry(entry.id)
    assert replayed_entry.state == DeadLetterState.REPLAYED
    assert replayed_entry.replayed_to_job_id == replay.id


async def test_in_process_worker_pool_runs_jobs_concurrently():
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    class Sleepy(Job):
        value: int

    @skrift.handler("sleepy")
    async def sleepy(job: Sleepy):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        await release.wait()
        active -= 1
        return job.value

    async with skrift.local_executor(mode="in_process", concurrency=2) as runtime:
        h1 = await runtime.submit(Sleepy(value=1))
        h2 = await runtime.submit(Sleepy(value=2))
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        release.set()
        assert sorted([await h1.result(timeout=1), await h2.result(timeout=1)]) == [1, 2]

    assert peak == 2


async def test_retry_exhaustion_dead_letters_and_raises():
    attempts = 0

    @skrift.handler("fail", max_attempts=2)
    async def fail(job: Greeting):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    runtime = skrift.configure_workers(mode="inline")
    handle = await runtime.submit(Greeting(name="Ada"))

    with pytest.raises(JobFailed, match="boom"):
        await handle.result()
    state = await handle.status()
    assert state.status == JobStatus.DEAD_LETTERED
    assert attempts == 2
    entries = await runtime.inspect_dlq()
    assert entries[0].cause == DeadLetterCause.RETRIES_EXHAUSTED
    assert entries[0].state == DeadLetterState.OPEN
    assert entries[0].attempts[-1].exception_type == "RuntimeError"


async def test_permanent_failure_requires_force_replay():
    @skrift.handler("permanent")
    async def permanent(job: Greeting):
        raise PermanentFailure("do not retry")

    runtime = skrift.configure_workers(mode="inline")
    handle = await runtime.submit(Greeting(name="Ada"))
    state = await handle.status()
    entries = await runtime.inspect_dlq()

    assert state.status == JobStatus.DEAD_LETTERED
    assert entries[0].cause == DeadLetterCause.PERMANENT_FAILURE
    with pytest.raises(PermissionError):
        await runtime.retry_dlq_entry(entries[0].id)

    replay = await runtime.retry_dlq_entry(entries[0].id, force=True)
    replay_state = await replay.status()
    replayed_entry = await runtime.get_dlq_entry(entries[0].id)
    assert replay_state.job.replayed_from == entries[0].id
    assert replayed_entry is not None
    assert replayed_entry.state == DeadLetterState.REPLAYED
    assert replayed_entry.replayed_to_job_id == replay.id


async def test_handler_on_dead_callback_runs_for_dead_lettered_job():
    seen = []

    @skrift.handler("dead_callback", max_attempts=1)
    async def dead_callback(payload: Greeting):
        raise RuntimeError(f"boom {payload.name}")

    @dead_callback.on_dead
    async def on_dead(entry: DeadJobEntry):
        seen.append((entry.job_type, entry.latest_error))

    runtime = skrift.configure_workers(mode="inline")
    handle = await runtime.submit(Greeting(name="Ada"))

    with pytest.raises(JobFailed, match="boom Ada"):
        await handle.result()

    assert seen == [("dead_callback", "RuntimeError: boom Ada")]


async def test_runtime_submit_accepts_idempotent_caller_supplied_job_id():
    @skrift.handler("idempotent_job")
    async def idempotent_job(payload: Greeting):
        return f"hi {payload.name}"

    runtime = skrift.configure_workers(mode="inline")
    first = await runtime.submit(Greeting(name="Ada"), job_id="fixed-job")
    second = await runtime.submit(Greeting(name="Ada"), job_id="fixed-job")

    assert first.id == second.id == "fixed-job"
    assert await second.result() == "hi Ada"
    with pytest.raises(JobIdConflict):
        await runtime.submit(Greeting(name="Grace"), job_id="fixed-job")


async def test_poison_payload_goes_to_dlq():
    runtime = WorkerRuntime(handler_registry=HandlerRegistry())
    runtime.registry.register("greet", lambda job: job.name, payload_model=Greeting)

    handle = await runtime.submit("greet", {"bad": "payload"})
    state = await handle.status()
    entries = await runtime.inspect_dlq()

    assert state.status == JobStatus.DEAD_LETTERED
    assert entries[0].cause == DeadLetterCause.POISON
    assert entries[0].job.id == handle.id


async def test_discard_dlq_entry_preserves_record():
    @skrift.handler("discard_me", max_attempts=1)
    async def discard_me(job: Greeting):
        raise RuntimeError("discardable")

    runtime = skrift.configure_workers(mode="inline")
    await runtime.submit(Greeting(name="Ada"))
    entry = (await runtime.inspect_dlq())[0]

    discarded = await runtime.discard_dlq_entry(entry.id, reason="triaged")
    stored = await runtime.get_dlq_entry(entry.id)

    assert discarded.state == DeadLetterState.DISCARDED
    assert stored is not None
    assert stored.discarded_reason == "triaged"


async def test_replay_clears_dead_lettered_queue_stat():
    should_fail = True

    @skrift.handler("replay_clears_dead", max_attempts=1)
    async def replay_clears_dead(job: Greeting):
        if should_fail:
            raise RuntimeError("first failure")
        return "ok"

    async with skrift.local_executor(mode="in_process") as runtime:
        handle = await runtime.submit(Greeting(name="Ada"))
        with pytest.raises(JobFailed, match="first failure"):
            await handle.result(timeout=1)
        entry = (await runtime.inspect_dlq())[0]
        stats = await runtime.queue.stats("default")
        assert stats.dead_lettered == 1

        should_fail = False
        replay = await runtime.retry_dlq_entry(entry.id)
        assert await replay.result(timeout=1) == "ok"
        stats = await runtime.queue.stats("default")
        assert stats.dead_lettered == 0


async def test_cancellation_before_claim():
    runtime = WorkerRuntime(
        config=WorkerConfig(mode="in_process"),
        handler_registry=HandlerRegistry(),
    )
    runtime.registry.register("greet", lambda job: "unused", payload_model=Greeting)
    handle = await runtime.submit("greet", {"name": "Ada"})

    assert await handle.cancel() is True
    state = await handle.status()
    assert state.status == JobStatus.CANCELLED


async def test_scheduled_pause_resumes_without_consuming_retry_attempt():
    class Pausing(Job):
        name: str

    @skrift.handler("pausing")
    async def pausing(job: Pausing, context):
        if not context.paused_state.get("resumed"):
            return Pause(
                resume_at=utcnow() + timedelta(seconds=0.02),
                state={"resumed": True},
            )
        return f"done {job.name}"

    async with skrift.local_executor(mode="in_process") as runtime:
        handle = await runtime.submit(Pausing(name="Ada"))
        assert await handle.result(timeout=1) == "done Ada"
        state = await handle.status()
        assert state.attempt == 1
        events = await runtime.event_log.read(LIFECYCLE_STREAM)

    assert "job_paused" in [event["type"] for _, event in events]
    assert "job_resumed" in [event["type"] for _, event in events]


def test_duplicate_handler_registration_fails():
    local = HandlerRegistry()
    local.register("greet", lambda job: "ok", payload_model=Greeting)

    with pytest.raises(ValueError):
        local.register("greet", lambda job: "nope", payload_model=Greeting)


async def test_unknown_job_type_fails_fast():
    runtime = skrift.configure_workers(mode="inline")

    with pytest.raises(KeyError):
        await runtime.submit("missing", {"name": "Ada"})


async def test_top_level_submit_uses_configured_runtime():
    @skrift.handler("greet")
    async def greet(job: Greeting):
        return job.name.upper()

    skrift.configure_workers(mode="inline")
    handle = await skrift.submit("greet", {"name": "Ada"})

    assert await handle.result() == "ADA"


async def test_wake_resumes_paused_job_without_schedule():
    class Manual(Job):
        name: str

    @skrift.handler("manual")
    async def manual(job: Manual, context):
        if not context.paused_state.get("awake"):
            return Pause(state={"awake": True})
        return f"awake {job.name}"

    async with skrift.local_executor(mode="in_process") as runtime:
        handle = await runtime.submit(Manual(name="Ada"))
        await asyncio.sleep(0.05)
        state = await handle.status()
        assert state.status == JobStatus.PAUSED
        snapshot = await runtime.inspect(job_limit=0)
        assert snapshot["jobs_active_total"] == 1
        assert await skrift.wake(handle.id) is True
        assert await handle.result(timeout=1) == "awake Ada"


async def test_runtime_inspect_returns_operator_snapshot():
    @skrift.handler("greet")
    async def greet(job: Greeting):
        return f"hello {job.name}"

    runtime = skrift.configure_workers(mode="inline")
    handle = await runtime.submit(Greeting(name="Ada"))
    snapshot = await runtime.inspect()

    assert snapshot["mode"] == "inline"
    assert snapshot["queues"][0].queue == "default"
    assert snapshot["queue_wait_bucket_seconds"] == 900
    assert snapshot["queue_trend_history"][0]["queues"][0]["queue"] == "default"
    assert snapshot["queue_wait_history"][0]["queues"][0]["queue"] == "default"
    assert snapshot["jobs"][0].job.id == handle.id
    assert snapshot["jobs_total"] == 1
    assert snapshot["jobs_active_total"] == 0
    assert snapshot["jobs_limit"] is None
    assert snapshot["handlers"][0].job_type == "greet"
    assert snapshot["events"][0][1]["type"] == "job_completed"


async def test_runtime_inspect_limits_operator_snapshot_records():
    @skrift.handler("limited_greet")
    async def limited_greet(job: Greeting):
        return f"hello {job.name}"

    runtime = skrift.configure_workers(mode="inline")
    for index in range(4):
        await runtime.submit(Greeting(name=f"Ada {index}"))

    snapshot = await runtime.inspect(job_limit=2, event_limit=3)

    assert len(snapshot["jobs"]) == 2
    assert snapshot["jobs_total"] == 4
    assert snapshot["jobs_limit"] == 2
    assert len(snapshot["events"]) == 3


async def test_completed_job_history_buckets_by_queue():
    @skrift.handler("history_greet")
    async def history_greet(job: Greeting):
        return f"hello {job.name}"

    runtime = skrift.configure_workers(mode="inline")
    await runtime.submit(Greeting(name="Ada"))

    history = await runtime.completed_job_history(bucket_count=4)
    assert sum(bucket["total"] for bucket in history) == 1
    assert sum(
        bucket["queues"].get("default", 0)
        for bucket in history
    ) == 1


async def test_queue_wait_history_is_bucketed_and_bounded():
    runtime = WorkerRuntime()

    await runtime.record_queue_history(
        queue_stats=[
            QueueStats(
                queue="default",
                ready=1,
                delayed=2,
                claimed=3,
                dead_lettered=4,
                oldest_ready_age_seconds=5,
            ),
        ],
    )
    await runtime.record_queue_history(
        queue_stats=[
            QueueStats(
                queue="default",
                ready=3,
                delayed=1,
                claimed=8,
                dead_lettered=0,
                oldest_ready_age_seconds=12,
            ),
        ],
    )

    history = await runtime.queue_wait_history()
    assert len(history) == 1
    assert history[0]["queues"][0]["ready"] == 3
    assert history[0]["queues"][0]["delayed"] == 1
    assert history[0]["queues"][0]["claimed"] == 8
    assert history[0]["queues"][0]["dead_lettered"] == 0
    assert history[0]["queues"][0]["oldest_ready_age_seconds"] == 12

    await runtime.record_queue_history(
        queue_stats=[
            QueueStats(
                queue="default",
                ready=0,
                delayed=0,
                claimed=0,
                dead_lettered=0,
                oldest_ready_age_seconds=4,
            ),
        ],
    )

    history = await runtime.queue_wait_history()
    assert len(history) == 1
    assert history[0]["queues"][0]["ready"] == 0
    assert history[0]["queues"][0]["oldest_ready_age_seconds"] == 12

    trend_history = await runtime.queue_trend_history()
    assert len(trend_history) == 1
    assert trend_history[0]["queues"][0]["ready"] == 3
    assert trend_history[0]["queues"][0]["delayed"] == 2
    assert trend_history[0]["queues"][0]["claimed"] == 8
    assert trend_history[0]["queues"][0]["dead_lettered"] == 4
    assert trend_history[0]["queues"][0]["oldest_ready_age_seconds"] == 12
    trend_timestamp = datetime.fromisoformat(trend_history[0]["timestamp"])
    assert (
        int(trend_timestamp.timestamp() * 1000)
        % int(runtime._queue_trend_bucket_seconds * 1000)
    ) == 0

    restored = WorkerRuntime(state_store=runtime.state_store)
    restored_history = await restored.queue_wait_history()
    assert restored_history == history

    for offset in range(runtime._queue_history_bucket_count + 5):
        timestamp = utcnow() + timedelta(
            seconds=offset * runtime._queue_history_bucket_seconds
        )
        bucket_start = runtime._queue_history_bucket_start(timestamp)
        sample = {
            "recorded_at": bucket_start,
            "timestamp": bucket_start.isoformat(),
            "queues": [{
                "queue": "default",
                "ready": offset,
                "delayed": offset,
                "claimed": offset,
                "dead_lettered": offset,
                "oldest_ready_age_seconds": offset,
            }],
        }
        async with runtime._queue_history_lock:
            runtime._queue_history.append(sample)
    async with runtime._queue_history_lock:
        await runtime._persist_queue_history_locked()

    assert len(await runtime.queue_wait_history()) <= runtime._queue_history_bucket_count
