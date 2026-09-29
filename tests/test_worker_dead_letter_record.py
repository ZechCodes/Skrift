"""A dead letter whose record the dead-letter store fails to save (#227), and
reconciling it from its pending marker (#232).

The job's state is already DEAD_LETTERED and its queue entry dead-lettered,
so nothing will run it again, and with no record it cannot be replayed. The
failure is logged at error level, naming the job, and still raised; no
``job_dead_lettered`` event or dead callback claims a record that does not
exist. The record waits in a pending marker, written before the record and
deleted once it is saved and announced, until ``reconcile_dead_letters``
finishes it. Each case runs on the in-memory, SQLite and (fake) Redis
backends.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.db.base import Base
from skrift.workers import PermanentFailure
from skrift.workers.models import DeadJobEntry, DeadLetterState, JobStatus
from skrift.workers.registry import handler, registry
from tests.test_worker_state_writes import (
    _record_lifecycle,
    redis_backends,
    sqlalchemy_backends,
)

QUEUE = "dead-letter-record"
PENDING = "workers:dead_letter_pending:"


class Item(BaseModel):
    n: int


class StoreDown(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def clean_worker_registry():
    registry.clear()
    yield
    registry.clear()
    skrift.configure_workers(mode="inline")


@pytest.fixture
async def worker_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dead_letter_record.db'}")
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


@pytest.fixture(params=["memory", "sqlalchemy", "redis"])
def backends(request, worker_session_maker, fake_redis_client):
    if request.param == "memory":
        return {}
    if request.param == "sqlalchemy":
        return sqlalchemy_backends(worker_session_maker)
    return redis_backends(fake_redis_client, "test:dead-letter-record")


def _register(dead, *, callback_fails=False):
    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        if payload.n == 0:
            raise PermanentFailure("no")
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        dead.append(entry)
        if callback_fails and len(dead) == 1:
            raise StoreDown("dead callback failed")


def _break_create(runtime, markers=None):
    """Make the record fail to save, noting the pending marker it finds."""
    state_store = runtime.state_store

    async def create(entry):
        if markers is not None:
            markers.append(await state_store.get(f"{PENDING}{entry.job.id}"))
        raise StoreDown("dead-letter store is down")

    runtime.dead_letter_store.create = create


def _restore_create(runtime):
    del runtime.dead_letter_store.create


async def _run(backends, path, *, markers=None, callback_fails=False, break_create=True):
    dead = []
    _register(dead, callback_fails=callback_fails)
    mode = "inline" if path == "inline" else "in_process"
    runtime = skrift.configure_workers(mode=mode, queues=(QUEUE,), poll_interval=0.01, **backends)
    events = _record_lifecycle(runtime)
    if break_create:
        _break_create(runtime, markers)
    job_id = f"{path}-{uuid4().hex}"
    raised = None
    if path == "queued":
        await runtime.submit("record.fails", Item(n=0), job_id=job_id)
        await runtime.start()
        try:
            for _ in range(500):
                state = await runtime.get_job_state(job_id)
                if state is not None and state.status == JobStatus.DEAD_LETTERED:
                    break
                await asyncio.sleep(0.01)
            # The worker loop logs the raised error once its run returns.
            await asyncio.sleep(0.1)
        finally:
            await runtime.stop()
    else:
        payload = {"n": "not a number"} if path == "poison" else Item(n=1)
        with pytest.raises(StoreDown) as info:
            await runtime.submit("record.fails", payload, job_id=job_id)
        raised = info.value
    state = await runtime.get_job_state(job_id)
    records = await _records(runtime, job_id)
    return runtime, job_id, raised, state, records, events, dead


async def _records(runtime, job_id):
    return [entry for entry in await runtime.dead_letter_store.list() if entry.job.id == job_id]


def _dead_lettered(events):
    return [event for event, _ in events if event == "job_dead_lettered"]


CAUSES = {"queued": "permanent_failure", "inline": "retries_exhausted", "poison": "poison"}


@pytest.mark.parametrize("path", list(CAUSES))
async def test_a_dead_letter_record_that_fails_to_save_is_logged_and_raised(backends, path, caplog):
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    markers = []
    runtime, job_id, raised, state, records, events, dead = await _run(
        backends, path, markers=markers
    )

    assert state.status == JobStatus.DEAD_LETTERED
    assert records == []
    assert _dead_lettered(events) == []
    assert dead == []

    # The marker was written before the record, and is kept.
    [marker] = markers
    assert isinstance(marker, DeadJobEntry)
    assert (marker.job.id, marker.cause.value) == (job_id, CAUSES[path])
    assert await runtime.state_store.get(f"{PENDING}{job_id}") == marker

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    [error] = errors
    message = error.getMessage()
    for text in (job_id, QUEUE, "record.fails", CAUSES[path], "DEAD_LETTERED"):
        assert text in message
    assert "no dead-letter record" in message
    assert "pending marker keeps the record" in message and "dlq reconcile" in message
    assert isinstance(error.exc_info[1], StoreDown)
    assert (error.job_id, error.queue, error.job_type, error.cause) == (
        job_id,
        QUEUE,
        "record.fails",
        CAUSES[path],
    )

    loop_errors = [
        record for record in caplog.records if record.getMessage().startswith("Worker loop error")
    ]
    if path == "queued":
        # The worker loop still reports the raised error, once.
        assert [type(record.exc_info[1]) for record in loop_errors] == [StoreDown]
    else:
        assert isinstance(raised, StoreDown)
        assert loop_errors == []


async def test_a_dead_letter_whose_marker_also_fails_says_it_cannot_be_replayed(backends, caplog):
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    dead = []
    _register(dead)
    runtime = skrift.configure_workers(mode="inline", queues=(QUEUE,), **backends)
    _break_create(runtime)
    state_store = runtime.state_store
    real_set = state_store.set

    async def set_(key, value, **kwargs):
        if key.startswith(PENDING):
            raise StoreDown("state store is down too")
        return await real_set(key, value, **kwargs)

    state_store.set = set_
    job_id = f"inline-{uuid4().hex}"
    with pytest.raises(StoreDown, match="dead-letter store is down"):
        await runtime.submit("record.fails", Item(n=1), job_id=job_id)

    assert await state_store.keys(PENDING) == []
    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert job_id in warning.getMessage() and "marker failed" in warning.getMessage()
    [error] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert job_id in error.getMessage() and "cannot be replayed" in error.getMessage()
    assert "dlq reconcile" not in error.getMessage()


async def test_a_saved_dead_letter_deletes_its_marker(backends):
    runtime, _, _, state, records, events, dead = await _run(
        backends, "queued", break_create=False
    )

    assert state.status == JobStatus.DEAD_LETTERED
    assert len(records) == 1
    assert len(_dead_lettered(events)) == 1 and len(dead) == 1
    assert await runtime.state_store.keys(PENDING) == []


@pytest.mark.parametrize("path", list(CAUSES))
async def test_reconcile_recreates_a_record_that_failed_to_save(backends, path):
    runtime, job_id, _, _, _, events, dead = await _run(backends, path)
    _restore_create(runtime)

    assert await runtime.reconcile_dead_letters() == {"recovered": [job_id], "failed": []}

    [entry] = await _records(runtime, job_id)
    assert entry.cause.value == CAUSES[path]
    assert entry.queue == QUEUE and entry.job_type == "record.fails"
    assert entry.state == DeadLetterState.OPEN
    assert len(_dead_lettered(events)) == 1
    assert [item.id for item in dead] == [entry.id]
    assert await runtime.state_store.keys(PENDING) == []

    # A second run has nothing left to do.
    assert await runtime.reconcile_dead_letters() == {"recovered": [], "failed": []}
    assert len(await _records(runtime, job_id)) == 1
    assert len(_dead_lettered(events)) == 1 and len(dead) == 1

    # The recreated record replays like any other.
    await runtime.retry_dlq_entry(entry.id, force=True)
    assert (await runtime.get_dlq_entry(entry.id)).state == DeadLetterState.REPLAYED


async def test_reconcile_finishes_a_dead_letter_whose_callback_failed(backends, caplog):
    """The record exists, so it is not created again (or reset), but the
    event and callback are delivered again: they are at-least-once."""
    runtime, job_id, _, _, records, events, dead = await _run(
        backends, "queued", callback_fails=True, break_create=False
    )
    [entry] = records
    assert await runtime.state_store.keys(PENDING) == [f"{PENDING}{job_id}"]
    await runtime.discard_dlq_entry(entry.id, reason="handled")

    assert await runtime.reconcile_dead_letters() == {"recovered": [job_id], "failed": []}

    [after] = await _records(runtime, job_id)
    assert after.id == entry.id and after.state == DeadLetterState.DISCARDED
    assert len(_dead_lettered(events)) == 2
    assert [item.id for item in dead] == [entry.id, entry.id]
    assert await runtime.state_store.keys(PENDING) == []


async def test_reconcile_keeps_a_marker_whose_record_still_fails(backends, caplog):
    runtime, job_id, _, _, _, events, dead = await _run(backends, "inline")
    caplog.clear()
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")

    assert await runtime.reconcile_dead_letters() == {"recovered": [], "failed": [job_id]}

    assert await _records(runtime, job_id) == []
    assert _dead_lettered(events) == [] and dead == []
    assert await runtime.state_store.keys(PENDING) == [f"{PENDING}{job_id}"]
    [error] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert job_id in error.getMessage() and isinstance(error.exc_info[1], StoreDown)

    _restore_create(runtime)
    assert await runtime.reconcile_dead_letters() == {"recovered": [job_id], "failed": []}
    assert len(await _records(runtime, job_id)) == 1


async def test_two_concurrent_reconciles_create_one_record(backends):
    runtime, job_id, _, _, _, events, dead = await _run(backends, "inline")
    _restore_create(runtime)
    # Both runs look for the record before either creates it.
    store = runtime.dead_letter_store
    real_get = store.get
    both_looked = asyncio.Barrier(2)
    looks = 0

    async def get(entry_id):
        nonlocal looks
        found = await real_get(entry_id)
        looks += 1
        if looks <= 2:
            await both_looked.wait()
        return found

    store.get = get

    first, second = await asyncio.wait_for(
        asyncio.gather(runtime.reconcile_dead_letters(), runtime.reconcile_dead_letters()),
        timeout=10,
    )

    assert first == second == {"recovered": [job_id], "failed": []}
    assert len(await _records(runtime, job_id)) == 1
    assert await runtime.state_store.keys(PENDING) == []
    # Each run delivered the event and callback.
    assert len(_dead_lettered(events)) == 2 and len(dead) == 2


async def test_worker_start_reconciles_before_it_polls(backends):
    runtime, job_id, *_ = await _run(backends, "queued")
    _restore_create(runtime)
    reconcile = runtime.reconcile_dead_letters
    pool_at_reconcile = []

    async def reconcile_dead_letters():
        pool_at_reconcile.append(runtime._pool)
        return await reconcile()

    runtime.reconcile_dead_letters = reconcile_dead_letters
    await runtime.start()
    try:
        assert pool_at_reconcile == [None]
        assert len(await _records(runtime, job_id)) == 1
        assert await runtime.state_store.keys(PENDING) == []
    finally:
        await runtime.stop()


async def test_worker_start_logs_a_failed_reconcile_and_starts(backends, caplog):
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    _register([])
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), poll_interval=0.01, **backends
    )
    real_keys = runtime.state_store.keys

    async def keys(prefix=""):
        if prefix == PENDING:
            raise StoreDown("state store is down")
        return await real_keys(prefix)

    runtime.state_store.keys = keys
    await runtime.start()
    try:
        assert runtime._pool is not None
        [error] = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert "Reconciling dead letters on worker start failed" in error.getMessage()
        assert isinstance(error.exc_info[1], StoreDown)
    finally:
        await runtime.stop()
