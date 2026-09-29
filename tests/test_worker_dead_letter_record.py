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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.db.base import Base
from skrift.workers import PermanentFailure
from skrift.workers.models import (
    DeadJobEntry,
    DeadLetterCause,
    DeadLetterState,
    JobEnvelope,
    JobStatus,
)
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
            markers.append(await state_store.get(_key(entry)))
        raise StoreDown("dead-letter store is down")

    runtime.dead_letter_store.create = create


def _restore_create(runtime):
    del runtime.dead_letter_store.create


def _key(entry):
    return f"{PENDING}{entry.job.id}:{entry.id}"


async def _no_reconcile():
    return {"recovered": [], "failed": []}


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
        # The start's own reconcile pass would race this run; it is tested
        # on its own below.
        runtime.reconcile_dead_letters = _no_reconcile
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
            del runtime.reconcile_dead_letters
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
    assert await runtime.state_store.keys(PENDING) == [_key(marker)]
    assert await runtime.state_store.get(_key(marker)) == marker

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
    assert await runtime.state_store.keys(PENDING) == [_key(entry)]
    await runtime.discard_dlq_entry(entry.id, reason="handled")

    assert await runtime.reconcile_dead_letters() == {"recovered": [job_id], "failed": []}

    [after] = await _records(runtime, job_id)
    assert after.id == entry.id and after.state == DeadLetterState.DISCARDED
    assert len(_dead_lettered(events)) == 2
    assert [item.id for item in dead] == [entry.id, entry.id]
    assert await runtime.state_store.keys(PENDING) == []


async def test_reconcile_keeps_a_marker_whose_record_still_fails(backends, caplog):
    runtime, job_id, _, _, _, events, dead = await _run(backends, "inline")
    [key] = await runtime.state_store.keys(PENDING)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")

    result = await runtime.reconcile_dead_letters()

    assert result["recovered"] == []
    [failed] = result["failed"]
    assert failed["job_id"] == job_id and failed["error"].startswith("StoreDown:")
    assert await _records(runtime, job_id) == []
    assert _dead_lettered(events) == [] and dead == []
    assert await runtime.state_store.keys(PENDING) == [key]
    [error] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert job_id in error.getMessage() and isinstance(error.exc_info[1], StoreDown)

    _restore_create(runtime)
    assert await runtime.reconcile_dead_letters() == {"recovered": [job_id], "failed": []}
    assert len(await _records(runtime, job_id)) == 1


async def test_reconcile_keeps_the_marker_of_a_job_with_no_handler(backends, caplog):
    """The record is created, but the event and dead callback wait, with the
    marker, until the handler is registered again."""
    runtime, job_id, _, _, _, events, dead = await _run(backends, "inline")
    _restore_create(runtime)
    [key] = await runtime.state_store.keys(PENDING)
    registry.clear()
    caplog.clear()
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")

    for _ in range(2):
        result = await runtime.reconcile_dead_letters()
        assert result == {
            "recovered": [],
            "failed": [
                {
                    "job_id": job_id,
                    "error": "no handler is registered for job type 'record.fails'",
                }
            ],
        }
    assert len(await _records(runtime, job_id)) == 1
    assert _dead_lettered(events) == [] and dead == []
    assert await runtime.state_store.keys(PENDING) == [key]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2  # one per pass
    assert all(job_id in w.getMessage() and "record.fails" in w.getMessage() for w in warnings)
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []

    _register(dead)
    assert await runtime.reconcile_dead_letters() == {"recovered": [job_id], "failed": []}
    assert len(await _records(runtime, job_id)) == 1
    assert len(_dead_lettered(events)) == 1 and len(dead) == 1
    assert await runtime.state_store.keys(PENDING) == []


async def test_a_dead_letter_deletes_only_its_own_marker(backends):
    """A job id can come back: its finished state expires and a new job takes
    the id. Its dead letter's marker survives the old one's completing."""
    dead = []
    _register(dead)
    runtime = skrift.configure_workers(mode="inline", queues=(QUEUE,), **backends)
    job = JobEnvelope(id=f"reused-{uuid4().hex}", type="record.fails", queue=QUEUE)
    store = runtime.dead_letter_store
    real_create = store.create
    creating = asyncio.Event()
    release = asyncio.Event()
    creates = 0

    async def create(entry):
        nonlocal creates
        creates += 1
        if creates == 1:
            creating.set()
            await release.wait()
            return await real_create(entry)
        raise StoreDown("dead-letter store is down")

    store.create = create

    def dead_letter(error):
        return runtime._dead_letter(
            job,
            cause=DeadLetterCause.RETRIES_EXHAUSTED,
            attempts=[],
            error=error,
            state_recorded=True,
        )

    old = asyncio.create_task(dead_letter("old"))
    await creating.wait()
    with pytest.raises(StoreDown):
        await dead_letter("new")
    release.set()
    old_entry = await old

    [key] = await runtime.state_store.keys(PENDING)
    marker = await runtime.state_store.get(key)
    assert marker.latest_error == "new" and marker.id != old_entry.id
    assert key == _key(marker)


async def test_a_dead_letter_store_rejects_a_repeated_create(backends):
    runtime = skrift.configure_workers(mode="inline", queues=(QUEUE,), **backends)
    store = runtime.dead_letter_store
    entry = DeadJobEntry(
        job=JobEnvelope(type="record.fails", queue=QUEUE),
        queue=QUEUE,
        job_type="record.fails",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
    )
    await store.create(entry)
    entry.state = DeadLetterState.DISCARDED
    await store.save(entry)

    # The SQLAlchemy store's unique entry id raises IntegrityError.
    with pytest.raises((ValueError, IntegrityError)):
        await store.create(entry.model_copy(update={"state": DeadLetterState.OPEN}))

    assert (await store.get(entry.id)).state == DeadLetterState.DISCARDED
    assert len(await store.list()) == 1


async def test_a_dead_letter_whose_record_was_saved_first_elsewhere_finishes(backends, caplog):
    """A reconcile can save the record while the dead letter itself is still
    creating it: the dead letter's create then fails on the duplicate id,
    which is not a missing record."""
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    dead = []
    _register(dead)
    runtime = skrift.configure_workers(mode="inline", queues=(QUEUE,), **backends)
    events = _record_lifecycle(runtime)
    store = runtime.dead_letter_store
    real_create = store.create

    async def create(entry):
        await real_create(entry)  # the reconcile gets there first
        return await real_create(entry)

    store.create = create
    job_id = f"raced-{uuid4().hex}"
    await runtime.submit("record.fails", Item(n=1), job_id=job_id)

    [entry] = await _records(runtime, job_id)
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    assert len(_dead_lettered(events)) == 1
    assert [item.id for item in dead] == [entry.id]
    assert await runtime.state_store.keys(PENDING) == []


async def test_a_held_reconcile_does_not_reset_a_record_acted_on_meanwhile(backends):
    runtime, job_id, *_ = await _run(backends, "inline")
    _restore_create(runtime)
    [key] = await runtime.state_store.keys(PENDING)
    marker = await runtime.state_store.get(key)
    store = runtime.dead_letter_store
    real_get = store.get
    looked = asyncio.Event()
    release = asyncio.Event()
    holding = True

    async def get(entry_id):
        nonlocal holding
        found = await real_get(entry_id)
        if holding:
            holding = False
            looked.set()
            await release.wait()
        return found

    store.get = get
    held = asyncio.create_task(runtime.reconcile_dead_letters())
    await looked.wait()
    # Meanwhile the record is created, and an operator discards it.
    await store.create(marker)
    await runtime.discard_dlq_entry(marker.id, reason="handled")
    release.set()

    assert await asyncio.wait_for(held, timeout=10) == {"recovered": [job_id], "failed": []}
    assert (await real_get(marker.id)).state == DeadLetterState.DISCARDED
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


async def test_worker_start_reconciles_once_the_pool_polls(backends):
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
        await asyncio.wait_for(runtime._reconcile_task, timeout=10)
        [pool] = pool_at_reconcile
        assert pool is not None
        assert len(await _records(runtime, job_id)) == 1
        assert await runtime.state_store.keys(PENDING) == []
    finally:
        await runtime.stop()


async def test_worker_start_does_not_wait_for_reconcile_and_stop_cancels_it(backends):
    _register([])
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), poll_interval=0.01, **backends
    )
    real_keys = runtime.state_store.keys
    scanning = asyncio.Event()
    cancelled = []

    async def keys(prefix=""):
        if prefix != PENDING:
            return await real_keys(prefix)
        scanning.set()
        try:
            await asyncio.Event().wait()  # a store that never answers
        except asyncio.CancelledError:
            cancelled.append(prefix)
            raise

    runtime.state_store.keys = keys
    await asyncio.wait_for(runtime.start(), timeout=5)
    task = runtime._reconcile_task
    try:
        assert runtime._pool is not None
        await asyncio.wait_for(scanning.wait(), timeout=5)
        assert not task.done()
    finally:
        await asyncio.wait_for(runtime.stop(), timeout=10)

    assert cancelled == [PENDING]
    assert task.done() and runtime._reconcile_task is None


async def test_a_dead_callback_can_wait_on_queued_work_during_the_start_pass(backends):
    followed = []
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), poll_interval=0.01, **backends
    )

    @handler("record.follow", queue=QUEUE)
    async def follow(payload: Item, context):
        return payload.n

    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        handle = await runtime.submit("record.follow", Item(n=7))
        followed.append(await handle.result(timeout=5))

    job = JobEnvelope(type="record.fails", queue=QUEUE, payload={"n": 1})
    entry = DeadJobEntry(
        job=job,
        queue=QUEUE,
        job_type="record.fails",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
        latest_error="RuntimeError: boom",
    )
    await runtime.state_store.set(_key(entry), entry)

    await asyncio.wait_for(runtime.start(), timeout=5)
    try:
        await asyncio.wait_for(runtime._reconcile_task, timeout=10)
    finally:
        await runtime.stop()

    assert followed == [7]
    assert [item.id for item in await _records(runtime, job.id)] == [entry.id]
    assert await runtime.state_store.keys(PENDING) == []


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
        await asyncio.wait_for(runtime._reconcile_task, timeout=10)
        [error] = [record for record in caplog.records if record.levelno >= logging.ERROR]
        assert "Reconciling dead letters on worker start failed" in error.getMessage()
        assert isinstance(error.exc_info[1], StoreDown)
    finally:
        await runtime.stop()


def _pending_entry():
    job = JobEnvelope(type="record.fails", queue=QUEUE, payload={"n": 1})
    return DeadJobEntry(
        job=job,
        queue=QUEUE,
        job_type="record.fails",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
        latest_error="RuntimeError: boom",
    )


async def test_a_dead_callback_can_stop_the_runtime_during_the_start_pass(backends, caplog):
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    called = []
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), poll_interval=0.01, **backends
    )

    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        called.append(entry.id)
        if len(called) == 1:
            await runtime.stop()

    entries = {entry.id: entry for entry in (_pending_entry(), _pending_entry())}
    for entry in entries.values():
        await runtime.state_store.set(_key(entry), entry)

    await asyncio.wait_for(runtime.start(), timeout=5)
    task = runtime._reconcile_task
    try:
        await asyncio.wait({task}, timeout=10)
        assert task.done() and not task.cancelled() and task.exception() is None
        assert runtime._pool is None
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

        # The called-back dead letter is finished; the other keeps its marker.
        [stopped_by] = called
        [left] = [entry for entry_id, entry in entries.items() if entry_id != stopped_by]
        assert await runtime.state_store.keys(PENDING) == [_key(left)]
        assert len(await _records(runtime, entries[stopped_by].job.id)) == 1
        assert await _records(runtime, left.job.id) == []

        await asyncio.wait_for(runtime.start(), timeout=5)
        await asyncio.wait_for(runtime._reconcile_task, timeout=10)
        assert called == [stopped_by, left.id]
        assert await runtime.state_store.keys(PENDING) == []
        assert len(await _records(runtime, left.job.id)) == 1
    finally:
        await asyncio.wait_for(runtime.stop(), timeout=10)


async def test_a_stop_from_elsewhere_cancels_the_start_pass_in_a_dead_callback(backends):
    in_callback = asyncio.Event()
    cancelled = []
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), poll_interval=0.01, **backends
    )

    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        in_callback.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(entry.id)
            raise

    entry = _pending_entry()
    await runtime.state_store.set(_key(entry), entry)

    await asyncio.wait_for(runtime.start(), timeout=5)
    task = runtime._reconcile_task
    try:
        await asyncio.wait_for(in_callback.wait(), timeout=10)
    finally:
        await asyncio.wait_for(runtime.stop(), timeout=10)

    assert cancelled == [entry.id]
    assert task.cancelled() and runtime._reconcile_task is None and runtime._pool is None
    assert await runtime.state_store.keys(PENDING) == [_key(entry)]


async def test_a_dead_callback_ignoring_its_cancellation_does_not_hold_up_stop(backends, caplog):
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    in_callback = asyncio.Event()
    release = asyncio.Event()
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=(QUEUE,),
        poll_interval=0.01,
        drain_timeout=0.05,
        drain_cancel_timeout=0.05,
        **backends,
    )

    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        in_callback.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # its own cleanup, deaf to the stop

    entry = _pending_entry()
    await runtime.state_store.set(_key(entry), entry)

    await asyncio.wait_for(runtime.start(), timeout=5)
    task = runtime._reconcile_task
    try:
        await asyncio.wait_for(in_callback.wait(), timeout=10)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(runtime.stop(), timeout=1)
        assert loop.time() - started < 0.5
        assert runtime._pool is None and not task.done()
        [warning] = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert "ignored its cancellation" in warning.getMessage()
    finally:
        release.set()
        await asyncio.wait({task}, timeout=10)
        await runtime.stop()

    # The callback swallowed its cancellation and returned, so it ran: its
    # dead letter is finished.
    assert task.done() and runtime._reconcile_task is None
    assert await runtime.state_store.keys(PENDING) == []


@pytest.mark.parametrize("held", ["record", "event"])
async def test_a_reconcile_leaves_its_dead_letter_when_the_runtime_stops_meanwhile(backends, held):
    dead = []
    _register(dead)
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), poll_interval=0.01, **backends
    )
    events = _record_lifecycle(runtime)
    entry = _pending_entry()
    await runtime.state_store.set(_key(entry), entry)
    holding, release = asyncio.Event(), asyncio.Event()
    target = runtime.dead_letter_store if held == "record" else runtime
    name = "create" if held == "record" else "emit_lifecycle"
    real = getattr(target, name)

    async def hold(*args, **kwargs):
        holding.set()
        await release.wait()
        return await real(*args, **kwargs)

    setattr(target, name, hold)
    # Not the start pass, so the stop does not cancel it.
    reconcile = asyncio.create_task(runtime.reconcile_dead_letters())
    await asyncio.wait_for(holding.wait(), timeout=10)
    await runtime.stop()
    release.set()

    assert await asyncio.wait_for(reconcile, timeout=10) == {"recovered": [], "failed": []}
    assert dead == []
    assert len(_dead_lettered(events)) == (1 if held == "event" else 0)
    assert len(await _records(runtime, entry.job.id)) == 1
    assert await runtime.state_store.keys(PENDING) == [_key(entry)]


async def _restart_while_a_callback_ignores_its_stop(backends):
    """Two markers; the first one's callback ignores the stop's cancellation,
    and the runtime starts again before that callback is released."""
    state = {"called": [], "running": 0, "most": 0}
    in_callback, release = asyncio.Event(), asyncio.Event()
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=(QUEUE,),
        poll_interval=0.01,
        drain_timeout=0.05,
        drain_cancel_timeout=0.05,
        **backends,
    )

    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        state["called"].append(entry.id)
        state["running"] += 1
        state["most"] = max(state["most"], state["running"])
        try:
            if len(state["called"]) == 1:
                in_callback.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            await asyncio.sleep(0)
        finally:
            state["running"] -= 1

    entries = [_pending_entry(), _pending_entry()]
    for entry in entries:
        await runtime.state_store.set(_key(entry), entry)

    await asyncio.wait_for(runtime.start(), timeout=5)
    old = runtime._reconcile_task
    await asyncio.wait_for(in_callback.wait(), timeout=10)
    await asyncio.wait_for(runtime.stop(), timeout=1)
    assert not old.done()
    await asyncio.wait_for(runtime.start(), timeout=5)
    new = runtime._reconcile_task
    assert new is not old and runtime._pool is not None
    return runtime, entries, state, release, old, new


async def test_a_restart_reconciles_what_a_pass_left_after_its_stop(backends):
    runtime, entries, state, release, old, new = await _restart_while_a_callback_ignores_its_stop(
        backends
    )
    try:
        await asyncio.sleep(0.1)
        assert len(state["called"]) == 1  # the new pass waits for the old one
        release.set()
        await asyncio.wait_for(asyncio.gather(old, new), timeout=10)
        assert runtime._pool is not None
        assert sorted(state["called"]) == sorted(entry.id for entry in entries)
        assert state["most"] == 1
        assert await runtime.state_store.keys(PENDING) == []
        for entry in entries:
            assert len(await _records(runtime, entry.job.id)) == 1
    finally:
        release.set()
        await asyncio.wait_for(runtime.stop(), timeout=10)


async def test_a_stop_while_a_restart_waits_on_the_old_pass_leaves_nothing_running(backends):
    runtime, entries, state, release, old, new = await _restart_while_a_callback_ignores_its_stop(
        backends
    )
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(runtime.stop(), timeout=1)
        assert loop.time() - started < 0.5
        assert new.cancelled() and runtime._pool is None and runtime._reconcile_task is None
        assert runtime._reconcile_tasks == {old}
    finally:
        release.set()
        await asyncio.wait({old}, timeout=10)

    assert old.done() and runtime._reconcile_tasks == set()
    [called] = state["called"]
    [left] = [entry for entry in entries if entry.id != called]
    assert await runtime.state_store.keys(PENDING) == [_key(left)]


STUBBORN_DEAD_CALLBACK = """
import asyncio
import pathlib

from pydantic import BaseModel

import skrift


class Item(BaseModel):
    n: int


@skrift.handler("record.fails", queue="dead-letter-record")
async def fails(payload: Item, context):
    raise RuntimeError("boom")


@fails.on_dead
async def on_dead(entry):
    pathlib.Path("called_back").touch()
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            if not pathlib.Path("stubborn").exists():
                raise
"""


@pytest.mark.parametrize("stubborn", [False, True], ids=["cooperative", "stubborn"])
async def test_a_worker_process_exits_past_a_dead_callback_ignoring_its_cancellation(
    tmp_path, worker_session_maker, stubborn
):
    import os
    import signal
    import sys
    import textwrap
    import time

    from skrift.workers import SQLAlchemyStateStore

    entry = _pending_entry()
    await SQLAlchemyStateStore(session_maker=worker_session_maker).set(_key(entry), entry)
    (tmp_path / "stubborn_dead.py").write_text(STUBBORN_DEAD_CALLBACK)
    if stubborn:
        (tmp_path / "stubborn").touch()
    (tmp_path / "app.yaml").write_text(
        textwrap.dedent(
            f"""
            db:
              url: sqlite+aiosqlite:///{tmp_path / "dead_letter_record.db"}
            workers:
              enabled: true
              preset: single_node
              queues: [{QUEUE}]
              drain_timeout: 0.1
              drain_cancel_timeout: 0.2
            """
        )
    )
    env = {**os.environ, "SECRET_KEY": "test-secret", "PYTHONPATH": str(tmp_path)}
    command = ["-m", "skrift", "-f", "app.yaml", "workers", "run", "--import", "stubborn_dead"]
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        *command,
        cwd=tmp_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        for _ in range(300):
            if (tmp_path / "called_back").exists() or worker.returncode is not None:
                break
            await asyncio.sleep(0.05)
        started = time.monotonic()
        worker.send_signal(signal.SIGTERM)
        output = (await asyncio.wait_for(worker.communicate(), 10))[0].decode()
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.communicate()

    assert (tmp_path / "called_back").exists(), output
    assert worker.returncode == 0, output
    assert time.monotonic() - started < 3, output
    forced = "reconcile pass still in a dead callback" in output
    assert forced == stubborn, output
    assert ("ignored its cancellation for" in output) == stubborn, output
    # The callback never returned, so its marker is kept either way.
    keys = await SQLAlchemyStateStore(session_maker=worker_session_maker).keys(PENDING)
    assert keys == [_key(entry)]


@pytest.mark.parametrize("bad_read", [0, 1], ids=["first", "later"])
async def test_a_marker_that_fails_to_read_is_reported_as_its_own_job(backends, bad_read):
    _register([])
    runtime = skrift.configure_workers(mode="in_process", queues=(QUEUE,), **backends)
    entries = {entry.job.id: entry for entry in (_pending_entry(), _pending_entry())}
    for entry in entries.values():
        await runtime.state_store.set(_key(entry), entry)
    real_get = runtime.state_store.get
    reads = []

    async def get(key):
        if key.startswith(PENDING):
            reads.append(key)
            if len(reads) == bad_read + 1:
                raise StoreDown("marker read failed")
        return await real_get(key)

    runtime.state_store.get = get
    result = await runtime.reconcile_dead_letters()
    del runtime.state_store.get

    bad = next(entry for entry in entries.values() if _key(entry) == reads[bad_read])
    [good] = [entry for entry in entries.values() if entry is not bad]
    assert result == {
        "recovered": [good.job.id],
        "failed": [{"job_id": bad.job.id, "error": "StoreDown: marker read failed"}],
    }
    assert await runtime.state_store.keys(PENDING) == [_key(bad)]
