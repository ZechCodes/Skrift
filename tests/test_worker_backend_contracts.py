"""Reusable smoke-test contracts for worker backend implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from skrift.db.base import Base
from skrift.db.models.worker import WorkerArchiveEventRecord, WorkerDeadLetterRecord
from skrift.workers import (
    DeadJobEntry,
    DeadLetterCause,
    DeadLetterState,
    InMemoryArchive,
    InMemoryEventLog,
    InMemoryQueue,
    InMemoryStateStore,
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    SQLAlchemyArchive,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
    WorkerPruner,
)
from skrift.workers.models import EventIdConflict, JobEnvelope, JobIdConflict, JobState, JobStatus, utcnow


@pytest.fixture
async def worker_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-contracts.db'}")
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


async def _assert_state_store_contract(store) -> None:
    await store.set("jobs:a", 1)
    await store.set("jobs:b", 2)
    await store.set("other:c", 3)

    async def increment(value):
        return (value or 0) + 4

    assert await store.update("jobs:a", increment) == 5
    assert await store.get("jobs:a") == 5
    assert await store.keys("jobs:") == ["jobs:a", "jobs:b"]

    state = JobState(job=JobEnvelope(type="contract"), status=JobStatus.SUBMITTED)
    await store.set("workers:jobs:contract", state)
    restored = await store.get("workers:jobs:contract")
    assert isinstance(restored, JobState)
    assert restored.status == JobStatus.SUBMITTED

    await store.delete("jobs:b")
    assert await store.get("jobs:b") is None
    assert await store.keys("jobs:") == ["jobs:a"]

    await store.set("jobs:short", "gone", ttl=0.01)
    await asyncio.sleep(0.02)
    assert await store.get("jobs:short") is None


async def _assert_event_log_contract(log) -> None:
    assert await log.append("contract", {"n": 1}) == 0
    assert await log.append("contract", {"n": 2}) == 1
    assert await log.append("contract", {"n": 3, "job_id": "job-1"}) == 2
    assert await log.append("contract", {"event_id": "evt-1", "n": 4}) == 3
    assert await log.append("contract", {"event_id": "evt-1", "n": 4}) == 3
    with pytest.raises(EventIdConflict):
        await log.append("contract", {"event_id": "evt-1", "n": 5})
    assert await log.read("contract", from_position=0, limit=2) == [
        (0, {"n": 1}),
        (1, {"n": 2}),
    ]
    assert await log.read_filtered("contract", filters={"job_id": "job-1"}) == [
        (2, {"n": 3, "job_id": "job-1"})
    ]

    read_tail = getattr(log, "read_tail", None)
    if callable(read_tail):
        assert await read_tail("contract", limit=2) == [
            (2, {"n": 3, "job_id": "job-1"}),
            (3, {"event_id": "evt-1", "n": 4}),
        ]

    subscription = log.subscribe("contract", from_position=4)
    next_event = asyncio.create_task(anext(subscription))
    await log.append("contract", {"n": 4})
    assert await asyncio.wait_for(next_event, timeout=1) == (4, {"n": 4})
    await subscription.aclose()

    await log.append("contract:one", {"n": 1})
    await log.append("contract:two", {"n": 2})
    await log.append("other", {"n": 3})
    assert await log.list_streams(prefix="contract") == [
        "contract",
        "contract:one",
        "contract:two",
    ]
    assert await log.list_streams(prefix="contract:") == ["contract:one", "contract:two"]

    await log.delete("contract")
    assert await log.read("contract") == []


async def _assert_queue_contract(queue) -> None:
    delayed = JobEnvelope(type="delayed", scheduled_for=utcnow() + timedelta(seconds=0.2))
    await queue.submit(delayed)
    assert await queue.claim(["default"], visibility_timeout=0.01) is None
    assert (await queue.stats("default")).delayed == 1
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
    assert (await queue.stats("default")).dead_lettered == 1

    ready = JobEnvelope(type="ready")
    assert (await queue.submit(ready)).id == ready.id
    assert (await queue.submit(ready.model_copy(deep=True))).id == ready.id
    with pytest.raises(JobIdConflict):
        await queue.submit(ready.model_copy(update={"type": "different"}))
    claimed_ready = await queue.claim(["default"], visibility_timeout=1)
    assert claimed_ready is not None
    assert claimed_ready.job.id == ready.id
    await queue.ack("default", ready.id, claimed_ready.token)
    assert (await queue.stats("default")).ready == 0

    cancellable = JobEnvelope(type="cancel")
    await queue.submit(cancellable)
    assert await queue.cancel("default", cancellable.id) is True
    assert await queue.cancel("default", cancellable.id) is False

    paused = JobEnvelope(type="pause", scheduled_for=utcnow() + timedelta(hours=1))
    await queue.submit(paused)
    assert (await queue.stats("default")).delayed == 1
    assert await queue.wake("default", paused.id) is True
    assert (await queue.stats("default")).ready == 1

    old_ready = JobEnvelope(type="old_ready", scheduled_for=utcnow() - timedelta(seconds=12))
    await queue.submit(old_ready)
    stats = await queue.stats("default")
    assert stats.ready >= 2
    assert stats.oldest_ready_age_seconds >= 12


async def _assert_archive_contract(archive) -> None:
    await archive.bulk_insert_events([("contract", 0, {"a": 1}), ("contract", 1, {"a": 2})])
    await archive.upsert_state_snapshot("job:1", {"state": "one"})
    await archive.upsert_state_snapshot("job:1", {"state": "two"})

    assert await archive.query_events("contract", from_position=1) == [(1, {"a": 2})]
    assert await archive.latest_state_snapshot("job:1") == {"state": "two"}
    assert len(await archive.historical_state_snapshots("job:1")) == 2


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda _: InMemoryStateStore(),
        lambda deps: SQLAlchemyStateStore(session_maker=deps["session_maker"]),
        lambda deps: RedisStateStore(client=deps["redis_client"], prefix="contract:state"),
    ],
    ids=["memory", "sqlalchemy", "redis"],
)
async def test_state_store_backend_contract(
    store_factory: Callable[[dict], object],
    worker_session_maker,
    fake_redis_client,
):
    await _assert_state_store_contract(
        store_factory({"session_maker": worker_session_maker, "redis_client": fake_redis_client})
    )


@pytest.mark.parametrize(
    "event_log_factory",
    [
        lambda _: InMemoryEventLog(),
        lambda deps: SQLAlchemyEventLog(session_maker=deps["session_maker"]),
        lambda deps: RedisEventLog(client=deps["redis_client"], prefix="contract:events"),
    ],
    ids=["memory", "sqlalchemy", "redis"],
)
async def test_event_log_backend_contract(
    event_log_factory: Callable[[dict], object],
    worker_session_maker,
    fake_redis_client,
):
    await _assert_event_log_contract(
        event_log_factory(
            {"session_maker": worker_session_maker, "redis_client": fake_redis_client}
        )
    )


@pytest.mark.parametrize(
    "queue_factory",
    [
        lambda _: InMemoryQueue(),
        lambda deps: SQLAlchemyQueue(session_maker=deps["session_maker"]),
        lambda deps: RedisQueue(client=deps["redis_client"], prefix="contract:queue"),
    ],
    ids=["memory", "sqlalchemy", "redis"],
)
async def test_queue_backend_contract(
    queue_factory: Callable[[dict], object],
    worker_session_maker,
    fake_redis_client,
):
    await _assert_queue_contract(
        queue_factory({"session_maker": worker_session_maker, "redis_client": fake_redis_client})
    )


@pytest.mark.parametrize(
    "archive_factory",
    [
        lambda _: InMemoryArchive(),
        lambda deps: SQLAlchemyArchive(session_maker=deps["session_maker"]),
    ],
    ids=["memory", "sqlalchemy"],
)
async def test_archive_backend_contract(
    archive_factory: Callable[[dict], object],
    worker_session_maker,
):
    await _assert_archive_contract(archive_factory({"session_maker": worker_session_maker}))


async def test_worker_pruner_runs_backend_retention_hooks(
    worker_session_maker,
    fake_redis_client,
):
    state_store = RedisStateStore(client=fake_redis_client, prefix="contract:prune")
    event_log = RedisEventLog(client=fake_redis_client, prefix="contract:prune")
    queue = RedisQueue(client=fake_redis_client, prefix="contract:prune")
    archive = SQLAlchemyArchive(session_maker=worker_session_maker)
    dead_letters = SQLAlchemyDeadLetterStore(session_maker=worker_session_maker)

    await event_log.append("workers:lifecycle", {"n": 1, "job_id": "job-1"})
    await event_log.append("workers:lifecycle", {"n": 2, "job_id": "job-2"})
    await event_log.append("workers:lifecycle", {"n": 3, "job_id": "job-3"})
    await event_log.append("agents:run:session-1", {"n": 1, "job_id": "agent-1"})
    await event_log.append("agents:run:session-1", {"n": 2, "job_id": "agent-2"})
    await state_store.set("workers:persister:event_cursors:workers:lifecycle", 3)
    await state_store.set("workers:persister:event_cursors:agents:run:session-1", 2)

    terminal_state = JobState(
        job=JobEnvelope(type="done"),
        status=JobStatus.COMPLETED,
    )
    await state_store.set("workers:jobs:done", terminal_state)

    dead_job = JobEnvelope(type="dead")
    await queue.submit(dead_job)
    claimed = await queue.claim(["default"], visibility_timeout=1)
    assert claimed is not None
    await queue.nack("default", dead_job.id, claimed.token, dead_letter=True)

    await archive.bulk_insert_events([("workers:lifecycle", 0, {"archived": True})])
    await archive.upsert_state_snapshot(
        "workers:queue_wait_history",
        {"queues": []},
        timestamp=utcnow() - timedelta(days=2),
    )

    dlq_entry = DeadJobEntry(
        job=JobEnvelope(type="resolved"),
        queue="default",
        job_type="resolved",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
        state=DeadLetterState.DISCARDED,
    )
    await dead_letters.create(dlq_entry)

    async with worker_session_maker() as session:
        await session.execute(
            update(WorkerArchiveEventRecord).values(created_at=utcnow() - timedelta(days=2))
        )
        await session.execute(
            update(WorkerDeadLetterRecord).values(
                entry_updated_at=utcnow() - timedelta(days=2),
            )
        )
        await session.commit()

    await asyncio.sleep(0.02)

    pruner = WorkerPruner(
        state_store=state_store,
        event_log=event_log,
        queue=queue,
        dead_letter_store=dead_letters,
        archive=archive,
        streams=("workers:lifecycle",),
        stream_prefixes=("agents:run",),
        retention=SimpleNamespace(
            redis_event_ttl=60,
            redis_event_max_entries=1,
            terminal_job_state_ttl=0.01,
            dead_queue_marker_ttl=0.01,
            archive_event_ttl=60,
            archive_snapshot_ttl=60,
            dlq_resolved_ttl=60,
            prune_interval=60,
        ),
    )

    counts = await pruner.prune_once()

    assert counts["redis_events"] == 3
    assert counts["terminal_job_states"] == 1
    assert counts["dead_queue_markers"] == 1
    assert counts["archive_events"] == 1
    assert counts["archive_snapshots"] == 1
    assert counts["dlq_resolved"] == 1
    assert await event_log.read("workers:lifecycle") == [(2, {"n": 3, "job_id": "job-3"})]
    assert await event_log.read("agents:run:session-1") == [(1, {"n": 2, "job_id": "agent-2"})]
    assert await state_store.get("workers:jobs:done") is None
    assert (await queue.stats("default")).dead_lettered == 0
