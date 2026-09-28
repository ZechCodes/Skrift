"""A worker whose claim expired and was taken over records nothing for the job (#185).

Worker A's handler outlives its claim; the reaper releases it and worker B
claims and runs the job. When A's handler finally returns or raises, A must not
write the job's state, emit lifecycle events or dead-letter it: all of that now
belongs to B.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
import skrift.workers.sqlalchemy as sqlalchemy_backend
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
from skrift.workers.models import JobEnvelope, JobStatus, utcnow
from skrift.workers.registry import registry

OUTCOME_EVENTS = {"job_failed", "job_paused", "job_completed", "job_dead_lettered"}


class Race(BaseModel):
    name: str


@pytest.fixture(autouse=True)
def clean_worker_registry():
    registry.clear()
    yield
    registry.clear()


@pytest.fixture
async def worker_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
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


def _backends(backend, session_maker, redis_client):
    if backend == "memory":
        return {}
    if backend == "sqlalchemy":
        return {
            "state_store": SQLAlchemyStateStore(session_maker=session_maker),
            "event_log": SQLAlchemyEventLog(session_maker=session_maker),
            "queue": SQLAlchemyQueue(session_maker=session_maker),
            "dead_letter_store": SQLAlchemyDeadLetterStore(session_maker=session_maker),
        }
    return {
        "state_store": RedisStateStore(client=redis_client, prefix="test:stale"),
        "event_log": RedisEventLog(client=redis_client, prefix="test:stale"),
        "queue": RedisQueue(client=redis_client, prefix="test:stale"),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


class TwoRuns:
    """Handler whose first run (worker A) ends with ``outcome`` once released and
    whose second run (worker B) completes once released."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.started = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]
        self.calls = 0

    async def __call__(self, job: Race):
        run = self.calls
        self.calls += 1
        self.started[run].set()
        await self.release[run].wait()
        if run == 1:
            return "B finished"
        if self.outcome == "fail":
            raise RuntimeError("A failed late")
        if self.outcome == "dead_letter":
            raise PermanentFailure("A failed late for good")
        if self.outcome == "pause":
            return Pause(state={"from": "A"})
        return "A finished late"


def _register(outcome):
    runs = TwoRuns(outcome)

    @skrift.handler("race", max_attempts=3)
    async def race(job: Race):
        return await runs(job)

    return runs


async def _outcome_events(runtime, job_id):
    return [
        event["type"]
        for _, event in await runtime.lifecycle_events_for_job(job_id)
        if event["type"] in OUTCOME_EVENTS
    ]


async def _stale_claim(runtime, handler):
    """A claims and starts the job; its claim expires and is reaped; B claims it."""
    handle = await runtime.submit(Race(name="ada"))
    claim_a = await runtime.queue.claim(["default"], visibility_timeout=0.05)
    worker_a = asyncio.create_task(runtime.execute_claim(claim_a))
    await handler.started[0].wait()
    await asyncio.sleep(0.1)
    await runtime.queue._release_expired_claims(utcnow())
    claim_b = await runtime.queue.claim(["default"], visibility_timeout=60)
    assert claim_b is not None
    return handle, worker_a, claim_b


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
@pytest.mark.parametrize("outcome", ["fail", "dead_letter", "pause", "succeed"])
async def test_stale_worker_leaves_the_new_owners_run_alone(
    backend, outcome, worker_session_maker, fake_redis_client
):
    handler = _register(outcome)
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    handle, worker_a, claim_b = await _stale_claim(runtime, handler)
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    await handler.started[1].wait()  # B's run is live
    b_running = await handle.status()

    handler.release[0].set()
    await worker_a  # A's late outcome is dropped, not raised

    # B's state is exactly as B wrote it: A wrote nothing over it.
    assert await handle.status() == b_running
    assert b_running.status == JobStatus.RUNNING
    assert await _outcome_events(runtime, handle.id) == []
    assert await runtime.inspect_dlq() == []

    handler.release[1].set()
    await worker_b
    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "B finished")
    assert await _outcome_events(runtime, handle.id) == ["job_completed"]


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
@pytest.mark.parametrize("outcome", ["fail", "dead_letter", "pause", "succeed"])
async def test_stale_worker_emits_nothing_before_the_new_owner_starts(
    backend, outcome, worker_session_maker, fake_redis_client
):
    # B has claimed the job but not yet recorded its run: A's claim is still
    # gone, so A must not announce an outcome or dead-letter the job.
    handler = _register(outcome)
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    handle, worker_a, claim_b = await _stale_claim(runtime, handler)

    handler.release[0].set()
    await worker_a

    assert await _outcome_events(runtime, handle.id) == []
    assert await runtime.inspect_dlq() == []
    # Nothing of A's run is left for B's run to start from: the state is back
    # to what it was before A started.
    state = await handle.status()
    assert (state.status, state.last_error, state.paused_state) == (JobStatus.SUBMITTED, None, {})

    handler.release[1].set()
    await runtime.execute_claim(claim_b)
    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "B finished")


RUN_EVENTS = {"job_claimed", "job_started", "job_resumed"} | OUTCOME_EVENTS


class InOrder:
    """Handler whose runs each wait for their own release, then return ``run <n>``
    or, for the runs in ``pause``, pause with ``{"run": n}``."""

    def __init__(self, pause=()):
        self.pause = set(pause)
        self.started = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]
        self.calls = 0

    async def __call__(self, job: Race):
        run = self.calls
        self.calls += 1
        self.started[run].set()
        await self.release[run].wait()
        if run in self.pause:
            return Pause(state={"run": run})
        return f"run {run}"


def _register_in_order(pause=()):
    runs = InOrder(pause)

    @skrift.handler("race", max_attempts=3)
    async def race(job: Race):
        return await runs(job)

    return runs


async def _within(awaitable):
    """Fail instead of hanging when a run is never released."""
    return await asyncio.wait_for(awaitable, 5)


async def _run_events(runtime, job_id):
    return [
        event["type"]
        for _, event in await runtime.lifecycle_events_for_job(job_id)
        if event["type"] in RUN_EVENTS
    ]


async def _expired_and_taken_over(runtime):
    """A receives a claim that expires before A starts on it; B reaps and claims."""
    handle = await runtime.submit(Race(name="ada"))
    claim_a = await runtime.queue.claim(["default"], visibility_timeout=0.05)
    await asyncio.sleep(0.1)
    await runtime.queue._release_expired_claims(utcnow())
    claim_b = await runtime.queue.claim(["default"], visibility_timeout=60)
    assert claim_b is not None
    return handle, claim_a, claim_b


class LateStart:
    """Worker A passes its expiry check just in time, then stalls before it
    writes RUNNING while its claim expires and worker B claims the job."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.stalled = asyncio.Event()
        self.release = asyncio.Event()
        self.task = None
        read_state = runtime.get_job_state

        async def get_job_state(job_id):
            if asyncio.current_task() is self.task and not self.release.is_set():
                self.stalled.set()
                await self.release.wait()
            return await read_state(job_id)

        runtime.get_job_state = get_job_state

    async def claims(self):
        runtime = self.runtime
        self.handle = await runtime.submit(Race(name="ada"))
        claim_a = await runtime.queue.claim(["default"], visibility_timeout=0.05)
        claim_a.visibility_timeout = 60  # A's check passes: it is not yet expired
        self.claim_a = claim_a
        self.worker_a = self.task = asyncio.create_task(runtime.execute_claim(claim_a))
        await _within(self.stalled.wait())
        await asyncio.sleep(0.1)
        await runtime.queue._release_expired_claims(utcnow())
        claim_b = await runtime.queue.claim(["default"], visibility_timeout=60)
        assert claim_b is not None
        return claim_b


def _hold_nack(runtime):
    """Hold every nack until the returned event is set."""
    held, release = asyncio.Event(), asyncio.Event()
    nack = runtime._nack

    async def held_nack(*args, **kwargs):
        held.set()
        await release.wait()
        return await nack(*args, **kwargs)

    runtime._nack = held_nack
    return held, release


async def _assert_paused_by_b(runtime, handle, b_run_id):
    state = await handle.status()
    assert (state.status, state.paused_state, state.run_id) == (
        JobStatus.PAUSED,
        {"run": 0},
        b_run_id,
    )
    assert await _outcome_events(runtime, handle.id) == ["job_paused"]
    stats = await runtime.queue.stats("default")
    assert (stats.ready, stats.delayed, stats.claimed) == (0, 1, 0)


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_worker_reaching_an_expired_claim_skips_the_run(
    backend, worker_session_maker, fake_redis_client, caplog
):
    handler = _register_in_order()
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    handle, claim_a, claim_b = await _expired_and_taken_over(runtime)
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    await _within(handler.started[0].wait())  # B's run is live

    with caplog.at_level(logging.WARNING, logger="skrift.workers.runtime"):
        await _within(runtime.execute_claim(claim_a))  # A resumes on its expired claim
    assert "after its claim expired" in caplog.text
    assert handler.calls == 1

    handler.release[0].set()
    await _within(worker_b)
    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "run 0")
    assert await _run_events(runtime, handle.id) == ["job_claimed", "job_started", "job_completed"]


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_late_worker_leaves_the_later_claims_run_alone(
    backend, worker_session_maker, fake_redis_client, caplog
):
    handler = _register_in_order()
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    late = LateStart(runtime)
    claim_b = await late.claims()
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    await _within(handler.started[0].wait())
    b_running = await late.handle.status()

    with caplog.at_level(logging.WARNING, logger="skrift.workers.runtime"):
        late.release.set()
        await _within(late.worker_a)
    assert "claimed again" in caplog.text
    assert handler.calls == 1
    assert await late.handle.status() == b_running

    handler.release[0].set()
    await _within(worker_b)
    state = await late.handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "run 0")
    assert await _run_events(runtime, late.handle.id) == [
        "job_claimed",
        "job_started",
        "job_completed",
    ]


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_late_worker_leaves_a_finished_run_finished(
    backend, worker_session_maker, fake_redis_client
):
    handler = _register_in_order()
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    late = LateStart(runtime)
    claim_b = await late.claims()
    handler.release[0].set()
    await _within(runtime.execute_claim(claim_b))

    late.release.set()
    await _within(late.worker_a)

    assert handler.calls == 1
    state = await late.handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "run 0")
    assert await runtime.wait_for_result(late.handle.id, timeout=1) == "run 0"
    assert await _outcome_events(runtime, late.handle.id) == ["job_completed"]


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_late_worker_leaves_a_pausing_run_paused(
    backend, worker_session_maker, fake_redis_client
):
    # B has written PAUSED and not yet nacked when A's late start arrives.
    handler = _register_in_order(pause={0})
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    late = LateStart(runtime)
    claim_b = await late.claims()
    nack_held, release_nack = _hold_nack(runtime)
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    handler.release[0].set()
    await _within(nack_held.wait())
    b_run_id = (await late.handle.status()).run_id

    late.release.set()
    await _within(late.worker_a)
    release_nack.set()
    await _within(worker_b)

    assert handler.calls == 1
    await _assert_paused_by_b(runtime, late.handle, b_run_id)


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_run_pauses_after_a_late_workers_start(
    backend, worker_session_maker, fake_redis_client
):
    # A's late start arrives while B's handler runs; B then pauses and must
    # still record its pause and nack its own claim.
    handler = _register_in_order(pause={0})
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    late = LateStart(runtime)
    claim_b = await late.claims()
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    await _within(handler.started[0].wait())
    b_run_id = (await late.handle.status()).run_id

    late.release.set()
    await asyncio.sleep(0.05)  # A's late start, if it runs, lands before B pauses
    handler.release[0].set()
    try:
        await _within(worker_b)
        await _assert_paused_by_b(runtime, late.handle, b_run_id)
    finally:
        handler.release[1].set()
        await _within(late.worker_a)


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_settled_outcome_wins_over_the_stored_state(
    backend, worker_session_maker, fake_redis_client
):
    # Once the queue accepts B's ack, B owns the job: its outcome is written
    # even over a state a later claim's run slipped in (as a store without an
    # atomic update can let happen, #195).
    handler = _register_in_order()
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    handle, _, claim_b = await _expired_and_taken_over(runtime)
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    await _within(handler.started[0].wait())
    stored = await handle.status()
    await runtime.state_store.set(
        runtime._job_key(handle.id),
        stored.model_copy(update={"run_id": "slipped-in", "run_order": stored.run_order + 1}),
    )

    handler.release[0].set()
    await _within(worker_b)
    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "run 0")


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_late_worker_with_an_equal_claim_order_leaves_a_pausing_run_paused(
    backend, worker_session_maker, fake_redis_client
):
    # Two claims can only share an order if a queue repeats one; A's start must
    # still not pass for B's own write.
    handler = _register_in_order(pause={0})
    runtime = skrift.configure_workers(
        mode="in_process", **_backends(backend, worker_session_maker, fake_redis_client)
    )
    late = LateStart(runtime)
    claim_b = await late.claims()
    late.claim_a.claim_order = claim_b.claim_order
    nack_held, release_nack = _hold_nack(runtime)
    worker_b = asyncio.create_task(runtime.execute_claim(claim_b))
    handler.release[0].set()
    await _within(nack_held.wait())
    b_run_id = (await late.handle.status()).run_id

    late.release.set()
    try:
        await asyncio.wait_for(asyncio.shield(late.worker_a), 1)
    except TimeoutError:
        handler.release[1].set()  # A started a run; let it end
        await _within(late.worker_a)
    release_nack.set()
    await _within(worker_b)

    assert handler.calls == 1
    await _assert_paused_by_b(runtime, late.handle, b_run_id)


async def _claim_retry_release_wake(queue):
    """Claim a job four times: after a nack, a reaped claim and a wake."""
    await queue.submit(JobEnvelope(type="race"))
    orders = []
    claimed = await queue.claim(["default"], visibility_timeout=60)
    orders.append(claimed.claim_order)
    await queue.nack("default", claimed.job.id, claimed.token, job=claimed.job)
    claimed = await queue.claim(["default"], visibility_timeout=0.05)
    orders.append(claimed.claim_order)
    await asyncio.sleep(0.1)
    await queue._release_expired_claims(utcnow())
    claimed = await queue.claim(["default"], visibility_timeout=60)
    orders.append(claimed.claim_order)
    await queue.nack(
        "default",
        claimed.job.id,
        claimed.token,
        job=claimed.job,
        retry_at=utcnow() + timedelta(hours=1),
    )
    assert await queue.wake("default", claimed.job.id)
    claimed = await queue.claim(["default"], visibility_timeout=60)
    orders.append(claimed.claim_order)
    return orders


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_each_claim_of_a_job_has_a_greater_order(
    backend, worker_session_maker, fake_redis_client
):
    queue = _backends(backend, worker_session_maker, fake_redis_client).get(
        "queue", skrift.workers.InMemoryQueue()
    )
    orders = await _claim_retry_release_wake(queue)
    assert orders == sorted(set(orders)) and None not in orders


async def test_sqlite_claim_order_grows_when_the_clock_steps_back(
    worker_session_maker, monkeypatch
):
    # The lease clock is wall time; a claim order taken from it would go
    # backwards here and the later claim would be skipped as superseded.
    attempts = []

    @skrift.handler("race", max_attempts=3)
    async def race(job: Race):
        attempts.append(job.name)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")
        return "second attempt"

    runtime = skrift.configure_workers(
        mode="in_process", **_backends("sqlalchemy", worker_session_maker, None)
    )
    handle = await runtime.submit(Race(name="ada"))
    first = await runtime.queue.claim(["default"], visibility_timeout=60)
    await _within(runtime.execute_claim(first))
    assert attempts == ["ada"]

    monkeypatch.setattr(
        sqlalchemy_backend,
        "_lease_clock",
        lambda session: sqlalchemy_backend.literal(
            sqlalchemy_backend._now() - timedelta(seconds=1),
            sqlalchemy_backend.DateTime(timezone=True),
        ),
    )
    second = await runtime.queue.claim(["default"], visibility_timeout=60)
    assert second.claim_order > first.claim_order
    await _within(runtime.execute_claim(second))

    assert attempts == ["ada", "ada"]
    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "second attempt")


async def test_redis_claim_order_survives_losing_every_other_key(fake_redis_client):
    # The order is kept on the job's own key, so it goes only with the job.
    queue = RedisQueue(client=fake_redis_client, prefix="test:order")
    await queue.submit(JobEnvelope(type="race"))
    first = await queue.claim(["default"], visibility_timeout=60)
    await queue.nack("default", first.job.id, first.token, job=first.job)
    second = await queue.claim(["default"], visibility_timeout=60)
    await queue.nack("default", second.job.id, second.token, job=second.job)

    keep = {queue._job_key(first.job.id), queue._ready_key("default")}
    for key in await fake_redis_client.keys("test:order:*"):
        if key.decode() not in keep:
            await fake_redis_client.delete(key)

    third = await queue.claim(["default"], visibility_timeout=60)
    assert first.claim_order < second.claim_order < third.claim_order
    assert third.job.id == first.job.id
