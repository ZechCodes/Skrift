"""Job state writes are ordered against the state-store updates they race (#217).

Submit, cancel, wake and DLQ replay used to write job state with a plain
``set``, after reading it separately; Redis pruning and index cleanup deleted
with no lock. A cancel could also be overwritten by a dead letter (#224). Each test holds one side of a race at the point where the other
used to slip in, on the in-memory, SQLite and (fake) Redis backends. The
scenarios take their backends as arguments so the integration tests can run
them on Postgres and a real Redis.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.db.base import Base
from skrift.workers import (
    InMemoryDeadLetterStore,
    Pause,
    PermanentFailure,
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
)
from skrift.workers.models import JobEnvelope, JobIdConflict, JobState, JobStatus, utcnow
from skrift.workers.registry import handler, registry

QUEUE = "state-writes"


class Item(BaseModel):
    n: int


@pytest.fixture(autouse=True)
def clean_worker_registry():
    registry.clear()
    yield
    registry.clear()
    skrift.configure_workers(mode="inline")


@pytest.fixture
async def worker_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'state_writes.db'}")
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
    return redis_backends(fake_redis_client, "test:state-writes")


def sqlalchemy_backends(session_maker):
    return {
        "state_store": SQLAlchemyStateStore(session_maker=session_maker),
        "event_log": SQLAlchemyEventLog(session_maker=session_maker),
        "queue": SQLAlchemyQueue(session_maker=session_maker),
        "dead_letter_store": SQLAlchemyDeadLetterStore(session_maker=session_maker),
    }


def redis_backends(client, prefix):
    return {
        "state_store": RedisStateStore(client=client, prefix=prefix),
        "event_log": RedisEventLog(client=client, prefix=prefix),
        "queue": RedisQueue(client=client, prefix=prefix),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


class HoldFirstRead:
    """State store whose first read of an armed key waits for ``release``: a
    ``get`` once it has read, and an ``update`` once its function has run (so
    while the update still holds the key)."""

    def __init__(self, store):
        self._store = store
        self._armed: tuple[str, tuple[str, ...]] | None = None
        self.read = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._store, name)

    def arm(self, key, reads=("get", "update")):
        self._armed = (key, reads)

    def _take(self, key, read):
        if self._armed is None or self._armed[0] != key or read not in self._armed[1]:
            return False
        self._armed = None
        return True

    async def _hold(self):
        self.read.set()
        await self.release.wait()

    async def get(self, key):
        value = await self._store.get(key)
        if self._take(key, "get"):
            await self._hold()
        return value

    async def update(self, key, fn, *, ttl=None):
        if not self._take(key, "update"):
            return await self._store.update(key, fn, ttl=ttl)

        async def held(current):
            try:
                value = fn(current)
                if inspect.isawaitable(value):
                    value = await value
            finally:
                await self._hold()
            return value

        return await self._store.update(key, held, ttl=ttl)


async def _within(awaitable, timeout=5):
    return await asyncio.wait_for(awaitable, timeout)


async def _let_run(task):
    """Give ``task`` 0.5 s to finish: a store that serialises it behind a held
    update keeps it waiting, one that doesn't lets it through."""
    await asyncio.wait({task}, timeout=0.5)


def _record_lifecycle(runtime):
    events = []
    emit = runtime.emit_lifecycle

    async def recording(event_type, job, **kwargs):
        events.append((event_type.value if hasattr(event_type, "value") else event_type, job.id))
        return await emit(event_type, job, **kwargs)

    runtime.emit_lifecycle = recording
    return events


def _register(runs=None):
    @handler("state.write", queue=QUEUE)
    async def write(payload: Item, context) -> str:
        if runs is not None:
            runs.append(payload.n)
        return "done"


# (a) submit


SUBMISSIONS = {
    "valid": Item(n=1),
    "other": Item(n=2),
    "poison": {"n": "not a number"},
}


async def run_concurrent_submits_of_one_id(backends, first, second):
    """Two submissions of one job id, the first held after reading the id's
    state: only one records the job, the other is told it exists."""
    _register()
    store = HoldFirstRead(backends.get("state_store") or skrift.workers.InMemoryStateStore())
    runtime = skrift.configure_workers(
        mode="in_process", queues=(QUEUE,), **{**backends, "state_store": store}
    )
    events = _record_lifecycle(runtime)
    job_id = f"dup-{uuid4().hex}"
    store.arm(runtime._job_key(job_id))

    first_task = asyncio.create_task(
        runtime.submit("state.write", SUBMISSIONS[first], job_id=job_id)
    )
    second_task = None
    try:
        await _within(store.read.wait())
        second_task = asyncio.create_task(
            runtime.submit("state.write", SUBMISSIONS[second], job_id=job_id)
        )
        await _let_run(second_task)
    finally:
        store.release.set()
    results = await _within(
        asyncio.gather(first_task, *([second_task] if second_task else []), return_exceptions=True)
    )

    state = await runtime.get_job_state(job_id)
    stats = await runtime.queue.stats(QUEUE)
    dead = await runtime.dead_letter_store.list(queue=QUEUE)
    recorded = [event for event, _ in events if event in {"job_submitted", "job_dead_lettered"}]
    return results, state, stats, dead, recorded


@pytest.mark.parametrize(
    "first,second",
    [("valid", "valid"), ("valid", "other"), ("valid", "poison"), ("poison", "poison")],
)
async def test_concurrent_submits_of_one_id_record_it_once(backends, first, second):
    results, state, stats, dead, recorded = await run_concurrent_submits_of_one_id(
        backends, first, second
    )
    assert_submitted_once(first, second, results, state, stats, dead, recorded)


def assert_submitted_once(first, second, results, state, stats, dead, recorded):
    # The held first submission wins: the second waits for it, then finds it.
    assert not isinstance(results[0], BaseException), results[0]
    if SUBMISSIONS[first] == SUBMISSIONS[second]:
        assert not isinstance(results[1], BaseException), results[1]
    else:
        assert isinstance(results[1], JobIdConflict), results[1]
    assert len(recorded) == 1
    if first == "poison":
        assert state.status == JobStatus.DEAD_LETTERED
        assert (stats.ready, len(dead)) == (0, 1)
    else:
        assert state.status == JobStatus.SUBMITTED
        assert state.job.payload == SUBMISSIONS[first].model_dump()
        assert (stats.ready, len(dead)) == (1, 0)


# (b) cancel


SETTLES = ("completes", "pauses")


async def run_a_cancel_racing_an_inline_run_that_settles_first(backends, settles="completes"):
    """A cancel reads SUBMITTED, then the inline run completes, or pauses,
    before it writes. The inline job has no queue entry to delete."""
    if settles == "pauses":

        @handler("state.write", queue=QUEUE)
        async def pause(payload: Item, context):
            return Pause()

    else:
        _register()
    runtime = skrift.configure_workers(mode="inline", **backends)
    events = _record_lifecycle(runtime)
    emit = runtime.emit_lifecycle
    submitted, run_go = asyncio.Event(), asyncio.Event()

    async def hold_submitted(event_type, job, **kwargs):
        await emit(event_type, job, **kwargs)
        if getattr(event_type, "value", event_type) == "job_submitted":
            submitted.set()
            await run_go.wait()

    runtime.emit_lifecycle = hold_submitted
    queue_cancel = runtime.queue.cancel
    cancel_read, cancel_go = asyncio.Event(), asyncio.Event()

    async def held_cancel(queue, job_id):
        cancelled = await queue_cancel(queue, job_id)
        cancel_read.set()
        await cancel_go.wait()
        return cancelled

    runtime.queue.cancel = held_cancel
    job_id = f"cancel-{uuid4().hex}"
    submit = asyncio.create_task(runtime.submit("state.write", Item(n=1), job_id=job_id))
    cancel = None
    try:
        await _within(submitted.wait())
        cancel = asyncio.create_task(runtime.cancel(job_id))
        await _within(cancel_read.wait())
        run_go.set()
        await _within(submit)
    finally:
        run_go.set()
        cancel_go.set()
    cancelled = await _within(cancel)
    return cancelled, await runtime.get_job_state(job_id), events


@pytest.mark.parametrize("settles", SETTLES)
async def test_a_cancel_does_not_overwrite_a_run_that_settled_first(backends, settles):
    cancelled, state, events = await run_a_cancel_racing_an_inline_run_that_settles_first(
        backends, settles
    )
    assert_inline_run_kept(settles, cancelled, state, events)


def assert_inline_run_kept(settles, cancelled, state, events):
    # Nothing was removed from a queue, so only a job still SUBMITTED would
    # be cancelled: a paused inline job is left to be woken.
    assert cancelled is False
    if settles == "pauses":
        assert state.status == JobStatus.PAUSED
    else:
        assert (state.status, state.result) == (JobStatus.COMPLETED, "done")
    assert "job_cancelled" not in [event for event, _ in events]


async def run_a_cancel_racing_a_held_start(backends):
    """An inline run's start holds the job's state; a cancel lands meanwhile."""
    runs = []
    _register(runs)
    store = HoldFirstRead(backends.get("state_store") or skrift.workers.InMemoryStateStore())
    runtime = skrift.configure_workers(mode="inline", **{**backends, "state_store": store})
    emit = runtime.emit_lifecycle
    job_id = f"start-{uuid4().hex}"

    async def arm_after_submitted(event_type, job, **kwargs):
        await emit(event_type, job, **kwargs)
        if getattr(event_type, "value", event_type) == "job_submitted":
            store.arm(runtime._job_key(job_id), reads=("update",))

    runtime.emit_lifecycle = arm_after_submitted
    submit = asyncio.create_task(runtime.submit("state.write", Item(n=1), job_id=job_id))
    cancel = None
    try:
        await _within(store.read.wait())
        cancel = asyncio.create_task(runtime.cancel(job_id))
        await _let_run(cancel)
    finally:
        store.release.set()
    await _within(submit)
    cancelled = await _within(cancel)
    return cancelled, runs, await runtime.get_job_state(job_id)


async def test_a_cancel_racing_a_runs_start_is_not_lost(backends):
    cancelled, runs, state = await run_a_cancel_racing_a_held_start(backends)
    # Either the cancel wins and the job never runs, or the run does and the
    # cancel reports it did nothing; never a cancel that the run ignores.
    assert (cancelled, runs, state.status) == (False, [1], JobStatus.COMPLETED)


CLAIMS = ("pause", "expire", "dead_letter")


async def run_a_cancel_racing_a_claim(backends, claim):
    """A cancel reads SUBMITTED and is held before its queue delete while a
    worker claims the job and: pauses it (nacked into the delayed queue); runs
    it until its claim expires and is reaped back to ready; or dead-letters it.
    The delete then succeeds on the unclaimed entry."""
    release_run = asyncio.Event()

    @handler("state.claimed", queue=QUEUE)
    async def claimed_job(payload: Item, context):
        if claim == "pause":
            return Pause()
        if claim == "dead_letter":
            raise PermanentFailure("no")
        await release_run.wait()
        return "stale"

    runtime = skrift.configure_workers(mode="in_process", queues=(QUEUE,), **backends)
    events = _record_lifecycle(runtime)
    job_id = f"claimed-{uuid4().hex}"
    await runtime.submit("state.claimed", Item(n=1), job_id=job_id)
    queue_cancel = runtime.queue.cancel
    cancel_read, cancel_go = asyncio.Event(), asyncio.Event()

    async def held_cancel(queue, ident):
        cancel_read.set()
        await cancel_go.wait()
        return await queue_cancel(queue, ident)

    runtime.queue.cancel = held_cancel
    cancel = asyncio.create_task(runtime.cancel(job_id))
    worker = None
    try:
        await _within(cancel_read.wait())
        visibility = 0.05 if claim == "expire" else 60
        claimed = await runtime.queue.claim([QUEUE], visibility_timeout=visibility)
        worker = asyncio.create_task(runtime.execute_claim(claimed))
        if claim == "expire":
            while (await runtime.get_job_state(job_id)).status != JobStatus.RUNNING:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            await runtime.queue._release_expired_claims(utcnow())
        else:
            await _within(worker)
    finally:
        cancel_go.set()
    cancelled = await _within(cancel)
    release_run.set()  # the expired run finishes after the cancel
    await _within(worker)
    stats = await runtime.queue.stats(QUEUE)
    left = (stats.ready, stats.delayed, stats.claimed)
    return cancelled, await runtime.get_job_state(job_id), left, events


@pytest.mark.parametrize("claim", CLAIMS)
async def test_a_cancel_whose_queue_delete_succeeds_settles_an_unsettled_job(backends, claim):
    cancelled, state, left, events = await run_a_cancel_racing_a_claim(backends, claim)
    assert_cancel_after_claim(claim, cancelled, state, left, events)


def assert_cancel_after_claim(claim, cancelled, state, left, events):
    # With its queue entry deleted the job never runs again: a paused or
    # running job is cancelled rather than stranded, a dead-lettered one kept.
    cancel_events = [event for event, _ in events if event == "job_cancelled"]
    assert left == (0, 0, 0)
    if claim == "dead_letter":
        assert (cancelled, state.status, cancel_events) == (False, JobStatus.DEAD_LETTERED, [])
    else:
        assert (cancelled, state.status, cancel_events) == (
            True,
            JobStatus.CANCELLED,
            ["job_cancelled"],
        )
        assert "job_completed" not in [event for event, _ in events]


DEAD_LETTERS = ("permanent_failure", "reclaim_loop")
DEAD_LETTER_HOLDS = ("after_nack", "before_record")


async def run_a_cancel_racing_a_dead_letter(backends, cause, hold):
    """A cancel reads SUBMITTED and is held before its queue delete while a
    worker claims the job and dead-letters it (its handler fails permanently,
    or the claim is over its reclaim limit). The run is held once its
    dead-letter nack has left the entry unclaimed, or later, just before the
    dead letter is recorded; the cancel's delete then succeeds (#224)."""

    @handler("state.dead", queue=QUEUE)
    async def dead_job(payload: Item, context):
        raise PermanentFailure("no")

    runtime = skrift.configure_workers(mode="in_process", queues=(QUEUE,), **backends)
    events = _record_lifecycle(runtime)
    job_id = f"dead-{uuid4().hex}"
    await runtime.submit("state.dead", Item(n=1), job_id=job_id)
    queue_cancel, queue_nack = runtime.queue.cancel, runtime.queue.nack
    create = runtime.dead_letter_store.create
    cancel_read, cancel_go = asyncio.Event(), asyncio.Event()
    run_held, run_go = asyncio.Event(), asyncio.Event()

    async def held_cancel(queue, ident):
        cancel_read.set()
        await cancel_go.wait()
        return await queue_cancel(queue, ident)

    async def held_nack(*args, **kwargs):
        await queue_nack(*args, **kwargs)
        if hold == "after_nack" and kwargs.get("dead_letter"):
            run_held.set()
            await run_go.wait()

    async def held_create(entry):
        if hold == "before_record":
            run_held.set()
            await run_go.wait()
        return await create(entry)

    runtime.queue.cancel, runtime.queue.nack = held_cancel, held_nack
    runtime.dead_letter_store.create = held_create
    cancel = asyncio.create_task(runtime.cancel(job_id))
    worker = None
    try:
        await _within(cancel_read.wait())
        claimed = await runtime.queue.claim([QUEUE], visibility_timeout=60)
        if cause == "reclaim_loop":
            claimed.job.reclaim_count = claimed.job.max_reclaims
        worker = asyncio.create_task(runtime.execute_claim(claimed))
        await _within(run_held.wait())
        cancel_go.set()
        cancelled = await _within(cancel)
    finally:
        cancel_go.set()
        run_go.set()
    await _within(worker)
    dead = [entry for entry in await runtime.dead_letter_store.list() if entry.job.id == job_id]
    replayed = None
    if dead:
        replay = await runtime.retry_dlq_entry(dead[0].id, force=True)
        replayed = (await runtime.queue.claim([QUEUE], visibility_timeout=60)).job.id == replay.id
    return cancelled, await runtime.get_job_state(job_id), len(dead), replayed, events


@pytest.mark.parametrize("hold", DEAD_LETTER_HOLDS)
@pytest.mark.parametrize("cause", DEAD_LETTERS)
async def test_a_cancel_racing_a_dead_letter_is_never_overwritten(backends, cause, hold):
    outcome = await run_a_cancel_racing_a_dead_letter(backends, cause, hold)
    assert_cancel_or_dead_letter(hold, *outcome)


def assert_cancel_or_dead_letter(hold, cancelled, state, dead, replayed, events):
    # Whichever of the cancel and the run's DEAD_LETTERED write lands first
    # wins: a cancel that returned True is never overwritten, and a job that
    # was dead-lettered keeps its dead letter, which can be replayed.
    kinds = [event for event, _ in events]
    if hold == "after_nack":
        assert (cancelled, state.status, dead) == (True, JobStatus.CANCELLED, 0)
        assert "job_dead_lettered" not in kinds and kinds.count("job_cancelled") == 1
    else:
        assert (cancelled, state.status, dead, replayed) == (
            False,
            JobStatus.DEAD_LETTERED,
            1,
            True,
        )
        assert "job_cancelled" not in kinds and kinds.count("job_dead_lettered") == 1


# (c) wake


async def run_two_wakes_of_a_paused_inline_then_queued_job(backends):
    """The first wake is held after reading PAUSED; the second resubmits the
    job, which a worker runs to completion before the first continues."""
    runs = []
    _register(runs)
    store = HoldFirstRead(backends.get("state_store") or skrift.workers.InMemoryStateStore())
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=(QUEUE,),
        poll_interval=0.01,
        **{**backends, "state_store": store},
    )
    job = JobEnvelope(
        id=f"wake-{uuid4().hex}",
        type="state.write",
        queue=QUEUE,
        payload={"n": 1},
        metadata={"skrift_dispatch": "inline_then_queued"},
    )
    await store.set(runtime._job_key(job.id), JobState(job=job, status=JobStatus.PAUSED))
    store.arm(runtime._job_key(job.id), reads=("get",))

    first = asyncio.create_task(runtime.wake(job.id))
    try:
        await _within(store.read.wait())
        second = await _within(runtime.wake(job.id))
        await runtime.start()
        await _within(runtime.wait_for_result(job.id))
    finally:
        store.release.set()
    try:
        woken = await _within(first)
        await asyncio.sleep(0.3)  # time for a second run, if the job was queued again
    finally:
        await runtime.stop()
    return (woken, second), runs, await runtime.get_job_state(job.id)


async def test_two_wakes_of_a_paused_job_queue_it_once(backends):
    woken, runs, state = await run_two_wakes_of_a_paused_inline_then_queued_job(backends)
    assert woken == (False, True)
    assert runs == [1]
    assert state.status == JobStatus.COMPLETED


async def run_a_scheduled_wake_of_a_paused_inline_job_racing_a_wake(backends):
    """The first wake, for a moment from now, is held after reading PAUSED; the
    second runs the job inline to completion before the first continues."""
    runs = []
    _register(runs)
    store = HoldFirstRead(backends.get("state_store") or skrift.workers.InMemoryStateStore())
    runtime = skrift.configure_workers(mode="inline", **{**backends, "state_store": store})
    job = JobEnvelope(
        id=f"wake-inline-{uuid4().hex}",
        type="state.write",
        queue=QUEUE,
        payload={"n": 1},
        metadata={"skrift_dispatch": "inline"},
    )
    await store.set(runtime._job_key(job.id), JobState(job=job, status=JobStatus.PAUSED))
    store.arm(runtime._job_key(job.id), reads=("get",))

    first = asyncio.create_task(
        runtime.wake(job.id, resume_at=utcnow() + timedelta(seconds=0.2))
    )
    try:
        await _within(store.read.wait())
        second = await _within(runtime.wake(job.id))
    finally:
        store.release.set()
    woken = await _within(first)
    return (woken, second), runs, await runtime.get_job_state(job.id)


async def test_a_scheduled_wake_does_not_rerun_a_job_another_wake_finished(backends):
    woken, runs, state = await run_a_scheduled_wake_of_a_paused_inline_job_racing_a_wake(
        backends
    )
    assert woken == (False, True)
    assert runs == [1]
    assert state.status == JobStatus.COMPLETED


# (d) Redis pruning and index cleanup


def _terminal(job_id, *, age=timedelta(hours=2), status=JobStatus.COMPLETED):
    job = JobEnvelope(id=job_id, type="state.write", queue=QUEUE, payload={"n": 1})
    return JobState(job=job, status=status, updated_at=utcnow() - age)


PRUNE_RACES = {
    # What the held update writes over the old terminal state.
    "rewritten": lambda state: state,
    "refreshed": lambda state: state.model_copy(update={"updated_at": utcnow()}),
    "running": lambda state: state.model_copy(update={"status": JobStatus.RUNNING}),
}


async def run_a_prune_racing_a_held_update(client, prefix, race):
    """An update holds an old terminal job state while the pruner runs."""
    store = RedisStateStore(client=client, prefix=prefix)
    key = "workers:jobs:pruned"
    await store.set(key, _terminal("pruned"))
    read, release = asyncio.Event(), asyncio.Event()

    async def held(value):
        read.set()
        await release.wait()
        return PRUNE_RACES[race](value)

    update = asyncio.create_task(store.update(key, held))
    prune = None
    try:
        await _within(read.wait())
        prune = asyncio.create_task(store.prune_terminal_job_states(max_age_seconds=3600))
        await _let_run(prune)
    finally:
        release.set()
    await _within(update)
    pruned = await _within(prune)
    return pruned, await store.get(key), await store.worker_job_counts()


@pytest.mark.parametrize("race", list(PRUNE_RACES))
async def test_a_prune_waits_for_an_update_and_rechecks_the_state(fake_redis_client, race):
    pruned, value, counts = await run_a_prune_racing_a_held_update(
        fake_redis_client, "test:prune", race
    )
    assert_prune_after_update(race, pruned, value, counts)


def assert_prune_after_update(race, pruned, value, counts):
    if race == "rewritten":
        # Still old and terminal once the update is done: pruned, not recreated.
        assert (pruned, value, counts["total"]) == (1, None, 0)
    else:
        assert pruned == 0
        assert value is not None and counts["total"] == 1


async def run_a_prune_whose_lock_expires_before_its_delete(client, prefix):
    """The pruner's lock expires after its recheck (simulated by deleting the
    lock key) and an update writes a running state before the delete."""
    store = RedisStateStore(client=client, prefix=prefix)
    key = "workers:jobs:lost-lock"
    stale = _terminal("lost-lock")
    await store.set(key, stale)
    get = store.get
    reads = []

    async def get_then_lose_the_lock(read_key):
        value = await get(read_key)
        reads.append(read_key)
        if read_key == key and len(reads) == 2:
            await client.delete(store._key("state", "locks", key))
            await store.update(key, lambda value: value.model_copy(update={"status": JobStatus.RUNNING}))
        return value

    store.get = get_then_lose_the_lock
    pruned = await store.prune_terminal_job_states(max_age_seconds=3600)
    del store.get
    return pruned, await store.get(key)


async def test_a_prune_that_lost_its_lock_deletes_nothing(fake_redis_client):
    pruned, value = await run_a_prune_whose_lock_expires_before_its_delete(
        fake_redis_client, "test:prune-lock"
    )
    assert pruned == 0
    assert value is not None and value.status == JobStatus.RUNNING


READERS = ["keys", "worker_job_states", "prune_terminal_job_states"]


async def run_an_index_cleanup_racing_a_recreating_update(client, prefix, reader):
    """A reader finds a job state's value gone (it expired) and drops the key
    from its indexes, but an update writes the value again in between."""
    store = RedisStateStore(client=client, prefix=prefix)
    key = "workers:jobs:recreated"
    await store.set(key, _terminal("recreated"))
    await client.delete(store._state_key(key))  # expired; the index entries remain

    async def recreate():
        await store.update(key, lambda value: _terminal("recreated", age=timedelta(0)))

    if reader == "keys":
        exists = client.exists

        async def exists_then_recreate(*names):
            found = await exists(*names)
            if names == (store._state_key(key),) and not found:
                await recreate()
            return found

        client.exists = exists_then_recreate
        try:
            await store.keys()
        finally:
            del client.exists
    else:
        get = store.get

        async def get_then_recreate(read_key):
            value = await get(read_key)
            if read_key == key and value is None:
                store.get = get
                await recreate()
            return value

        store.get = get_then_recreate
        try:
            if reader == "worker_job_states":
                await store.worker_job_states()
            else:
                await store.prune_terminal_job_states(max_age_seconds=3600)
        finally:
            store.get = get
    states, _ = await store.worker_job_states()
    return key in await store.keys(), [state.job.id for state in states], await store.worker_job_counts()


@pytest.mark.parametrize("reader", READERS)
async def test_an_index_cleanup_keeps_a_value_an_update_recreated(fake_redis_client, reader):
    listed, states, counts = await run_an_index_cleanup_racing_a_recreating_update(
        fake_redis_client, "test:forget", reader
    )
    assert (listed, states, counts["total"]) == (True, ["recreated"], 1)


# (e) SQLAlchemy expired-row get


class UpdateAfterFirstRead:
    """Session maker whose first session runs ``between`` after its first read."""

    def __init__(self, session_maker, between):
        self._session_maker = session_maker
        self._between = between

    def __call__(self):
        session = self._session_maker()
        execute = session.execute

        async def execute_then_update(statement, *args, **kwargs):
            result = await execute(statement, *args, **kwargs)
            if self._between is not None and not getattr(statement, "is_dml", False):
                between, self._between = self._between, None
                await between()
            return result

        session.execute = execute_then_update
        return session


async def run_an_expired_get_racing_an_update(session_maker):
    """``get`` reads an expired row; an update rewrites it before ``get`` deletes it."""
    store = SQLAlchemyStateStore(session_maker=session_maker)
    key = f"runstate:{uuid4().hex}"
    await store.set(key, ["old"], ttl=0.001)
    await asyncio.sleep(0.05)

    async def refresh():
        await store.update(key, lambda value: ["fresh"])

    reading = SQLAlchemyStateStore(session_maker=UpdateAfterFirstRead(session_maker, refresh))
    read = await reading.get(key)
    return read, await store.get(key)


async def test_an_expired_get_does_not_delete_a_row_an_update_refreshed(worker_session_maker):
    read, value = await run_an_expired_get_racing_an_update(worker_session_maker)
    assert (read, value) == (None, ["fresh"])
