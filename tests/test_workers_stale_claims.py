"""A worker whose claim expired and was taken over records nothing for the job (#185).

Worker A's handler outlives its claim; the reaper releases it and worker B
claims and runs the job. When A's handler finally returns or raises, A must not
write the job's state, emit lifecycle events or dead-letter it: all of that now
belongs to B.
"""

from __future__ import annotations

import asyncio

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
from skrift.workers.models import JobStatus, utcnow
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
    # Nothing of A's late outcome is left for B's run to start from.
    state = await handle.status()
    assert (state.status, state.last_error, state.paused_state) == (JobStatus.RUNNING, None, {})

    handler.release[1].set()
    await runtime.execute_claim(claim_b)
    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, "B finished")
