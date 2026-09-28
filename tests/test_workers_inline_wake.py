"""One wake of a paused inline job runs it once (#219).

An inline job has no queue entry to claim, so two wakes of one pause used to
both run it: each read PAUSED and called ``execute_claim``, and an inline run's
start accepts any stored state. Each test holds one wake where the other used
to slip in, on the in-memory, SQLite and (fake) Redis backends. A scheduled
wake also gives the job up to a cancel during its wait.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.db.base import Base
from skrift.workers import (
    InMemoryDeadLetterStore,
    Pause,
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
)
from skrift.workers.models import JobStatus, utcnow
from skrift.workers.registry import handler, registry
from skrift.workers.runtime import LIFECYCLE_STREAM


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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inline_wake.db'}")
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
        return {
            "state_store": SQLAlchemyStateStore(session_maker=worker_session_maker),
            "event_log": SQLAlchemyEventLog(session_maker=worker_session_maker),
            "queue": SQLAlchemyQueue(session_maker=worker_session_maker),
            "dead_letter_store": SQLAlchemyDeadLetterStore(session_maker=worker_session_maker),
        }
    prefix = "test:inline-wake"
    return {
        "state_store": RedisStateStore(client=fake_redis_client, prefix=prefix),
        "event_log": RedisEventLog(client=fake_redis_client, prefix=prefix),
        "queue": RedisQueue(client=fake_redis_client, prefix=prefix),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


class PausedJob:
    """An inline job that pauses ``pauses`` times, then waits for ``finish``.
    Each run records the paused state it sees."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.runs = []
        self.resumed = asyncio.Event()
        self.finish = asyncio.Event()

    @classmethod
    async def paused(cls, backends, pauses=1):
        job = cls(skrift.configure_workers(mode="inline", **backends))

        @handler("inline.wake", queue="inline-wake")
        async def pause(item: Item, context):
            job.runs.append(dict(context.paused_state))
            if len(job.runs) <= pauses:
                return Pause(state={"step": len(job.runs)})
            job.resumed.set()
            await job.finish.wait()
            return "done"

        await job.runtime.submit_inline(
            "inline.wake", Item(n=1), job_id="job", metadata={"skrift_dispatch": "inline"}
        )
        assert (await job.runtime.get_job_state("job")).status == JobStatus.PAUSED
        return job

    def hold_next_read(self):
        """Hold the next ``get_job_state`` (a wake's read) until ``release``."""
        get_job_state = self.runtime.get_job_state
        read, release = asyncio.Event(), asyncio.Event()

        async def held(job_id):
            self.runtime.get_job_state = get_job_state
            state = await get_job_state(job_id)
            read.set()
            await release.wait()
            return state

        self.runtime.get_job_state = held
        return read, release

    async def outcome(self):
        state = await self.runtime.get_job_state("job")
        events = [
            event["type"]
            for _, event in await self.runtime.event_log.read(LIFECYCLE_STREAM)
            if event["job_id"] == "job"
        ]
        return self.runs, state.status, events.count("job_resumed")


def _soon():
    return utcnow() + timedelta(seconds=0.3)


async def _within(awaitable, timeout=5):
    return await asyncio.wait_for(awaitable, timeout)


async def test_two_wakes_of_a_pause_run_the_job_once(backends):
    job = await PausedJob.paused(backends)
    read, release = job.hold_next_read()
    first = asyncio.create_task(job.runtime.wake("job"))
    await _within(read.wait())  # the first wake read PAUSED
    second = asyncio.create_task(job.runtime.wake("job"))
    await _within(job.resumed.wait())  # the second wake's run is live

    release.set()
    await asyncio.wait({first}, timeout=0.5)
    job.finish.set()
    woken = await _within(asyncio.gather(first, second))

    assert woken == [False, True]
    assert await job.outcome() == ([{}, {"step": 1}], JobStatus.COMPLETED, 1)


async def test_two_scheduled_wakes_of_a_pause_run_the_job_once(backends):
    job = await PausedJob.paused(backends)
    job.finish.set()
    read, release = job.hold_next_read()
    first = asyncio.create_task(job.runtime.wake("job", resume_at=_soon()))
    await _within(read.wait())  # the first wake read PAUSED
    second = asyncio.create_task(job.runtime.wake("job", resume_at=_soon()))
    await asyncio.wait({second}, timeout=0.1)  # the second is scheduled

    release.set()
    woken = await _within(asyncio.gather(first, second))

    assert woken == [False, True]
    assert await job.outcome() == ([{}, {"step": 1}], JobStatus.COMPLETED, 1)


async def test_a_wake_of_a_scheduled_pause_does_not_run_it_early(backends):
    job = await PausedJob.paused(backends)
    job.finish.set()
    scheduled = asyncio.create_task(job.runtime.wake("job", resume_at=_soon()))
    await asyncio.wait({scheduled}, timeout=0.1)  # the wake is scheduled

    assert await job.runtime.wake("job") is False
    assert job.runs == [{}]
    assert await _within(scheduled) is True
    assert await job.outcome() == ([{}, {"step": 1}], JobStatus.COMPLETED, 1)


async def test_a_wake_of_an_earlier_pause_does_not_resume_a_later_one(backends):
    job = await PausedJob.paused(backends, pauses=2)
    job.finish.set()
    read, release = job.hold_next_read()
    first = asyncio.create_task(job.runtime.wake("job"))
    await _within(read.wait())  # the first wake read the first pause

    assert await job.runtime.wake("job") is True  # resumed, and paused again
    release.set()
    assert await _within(first) is False
    state = await job.runtime.get_job_state("job")
    assert (state.status, state.paused_state) == (JobStatus.PAUSED, {"step": 2})

    assert await job.runtime.wake("job") is True
    assert await job.outcome() == ([{}, {"step": 1}, {"step": 2}], JobStatus.COMPLETED, 2)


async def test_a_cancel_during_a_scheduled_wake_keeps_the_job_cancelled(backends):
    job = await PausedJob.paused(backends)
    job.finish.set()
    scheduled = asyncio.create_task(job.runtime.wake("job", resume_at=_soon()))
    await asyncio.wait({scheduled}, timeout=0.1)  # the wake is scheduled
    assert (await job.runtime.get_job_state("job")).status == JobStatus.SUBMITTED

    assert await job.runtime.cancel("job") is True
    assert await _within(scheduled) is False
    events = [
        event["type"]
        for _, event in await job.runtime.event_log.read(LIFECYCLE_STREAM)
        if event["job_id"] == "job"
    ]
    assert "job_resumed" not in events[events.index("job_cancelled") :]
    assert await job.outcome() == ([{}], JobStatus.CANCELLED, 0)
