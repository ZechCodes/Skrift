"""One wake of a paused inline job runs it once (#219).

An inline job has no queue entry to claim, so two wakes of one pause used to
both run it: each read PAUSED and called ``execute_claim``, and an inline run's
start accepts any stored state. Each test holds one wake where the other used
to slip in, on the in-memory, SQLite and (fake) Redis backends. A wake also
gives the job up to a cancel before its run starts, and never runs a job
that replaced its own under the same id.
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
    Each run records the paused state it sees, and its payload; a job with
    payload 2 always pauses."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.runs = []
        self.payloads = []
        self.resumed = asyncio.Event()
        self.finish = asyncio.Event()

    @classmethod
    async def paused(cls, backends, pauses=1, **config):
        job = cls(skrift.configure_workers(mode="inline", **backends, **config))

        @handler("inline.wake", queue="inline-wake")
        async def pause(item: Item, context):
            job.runs.append(dict(context.paused_state))
            job.payloads.append(item.n)
            if item.n == 2 or len(job.runs) <= pauses:
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

    def hold_next(self, name):
        """Hold the next call of the runtime's ``name`` until ``release``."""
        original = getattr(self.runtime, name)
        reached, release = asyncio.Event(), asyncio.Event()

        async def held(*args, **kwargs):
            setattr(self.runtime, name, original)
            reached.set()
            await release.wait()
            return await original(*args, **kwargs)

        setattr(self.runtime, name, held)
        return reached, release

    async def events(self):
        return [
            event["type"]
            for _, event in await self.runtime.event_log.read(LIFECYCLE_STREAM)
            if event["job_id"] == "job"
        ]

    async def outcome(self):
        state = await self.runtime.get_job_state("job")
        return self.runs, state.status, (await self.events()).count("job_resumed")


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
    events = await job.events()
    assert "job_resumed" not in events[events.index("job_cancelled") :]
    assert await job.outcome() == ([{}], JobStatus.CANCELLED, 0)


def _hold_the_wait(monkeypatch):
    """Hold a scheduled wake's wait (in a task named "wake") until ``release``."""
    sleep = asyncio.sleep
    reached, release = asyncio.Event(), asyncio.Event()

    async def held(delay, *args, **kwargs):
        if asyncio.current_task().get_name() != "wake":
            return await sleep(delay, *args, **kwargs)
        reached.set()
        await release.wait()

    monkeypatch.setattr(asyncio, "sleep", held)
    return reached, release


WAKES = ("immediate", "scheduled")


@pytest.mark.parametrize("wake", WAKES)
async def test_a_cancel_before_a_wakes_run_starts_keeps_the_job_cancelled(backends, wake):
    job = await PausedJob.paused(backends)
    job.finish.set()
    paused_events = await job.events()
    reached, release = job.hold_next("_start_run")
    resume_at = _soon() if wake == "scheduled" else None
    woken = asyncio.create_task(job.runtime.wake("job", resume_at=resume_at))
    await _within(reached.wait())  # the wake is about to start its run

    assert await job.runtime.cancel("job") is True
    release.set()
    assert await _within(woken) is False
    assert await job.outcome() == ([{}], JobStatus.CANCELLED, 0)
    assert await job.events() == [*paused_events, "job_cancelled"]


REPLACED = ("before_immediate_start", "before_scheduled_start", "during_scheduled_wait")


@pytest.mark.parametrize("replaced", REPLACED)
async def test_a_wake_does_not_run_a_job_that_replaced_its_own(backends, monkeypatch, replaced):
    """A cancel during the wake's hold, whose CANCELLED state expires; the
    same id is then submitted again, and pauses."""
    job = await PausedJob.paused(backends, terminal_job_state_ttl=0.05)
    if replaced == "during_scheduled_wait":
        reached, release = _hold_the_wait(monkeypatch)
    else:
        reached, release = job.hold_next("execute_claim")
    resume_at = None if replaced == "before_immediate_start" else _soon()
    woken = asyncio.create_task(job.runtime.wake("job", resume_at=resume_at), name="wake")
    await _within(reached.wait())

    assert await job.runtime.cancel("job") is True
    while await job.runtime.get_job_state("job") is not None:
        await asyncio.sleep(0.01)
    await job.runtime.submit_inline(
        "inline.wake", Item(n=2), job_id="job", metadata={"skrift_dispatch": "inline"}
    )
    replacement = await job.runtime.get_job_state("job")
    assert (replacement.status, replacement.job.payload) == (JobStatus.PAUSED, {"n": 2})
    events = await job.events()

    release.set()
    assert await _within(woken) is False
    assert await job.runtime.get_job_state("job") == replacement
    assert await job.events() == events
    assert job.payloads == [1, 2]


async def test_a_wake_does_not_run_a_resubmitted_job_before_its_run(backends, monkeypatch):
    """As above, with the resubmitted job held before its run: SUBMITTED, as
    the wake's own state was."""
    job = await PausedJob.paused(backends, terminal_job_state_ttl=0.05)
    reached, release = _hold_the_wait(monkeypatch)
    woken = asyncio.create_task(job.runtime.wake("job", resume_at=_soon()), name="wake")
    await _within(reached.wait())

    assert await job.runtime.cancel("job") is True
    while await job.runtime.get_job_state("job") is not None:
        await asyncio.sleep(0.01)
    submitted, submit_go = job.hold_next("execute_claim")
    resubmit = asyncio.create_task(
        job.runtime.submit_inline(
            "inline.wake", Item(n=2), job_id="job", metadata={"skrift_dispatch": "inline"}
        )
    )
    await _within(submitted.wait())
    replacement = await job.runtime.get_job_state("job")
    assert (replacement.status, replacement.job.payload) == (JobStatus.SUBMITTED, {"n": 2})
    events = await job.events()

    release.set()
    assert await _within(woken) is False
    assert await job.runtime.get_job_state("job") == replacement
    assert await job.events() == events
    submit_go.set()
    await _within(resubmit)
    assert job.payloads == [1, 2]
    assert (await job.runtime.get_job_state("job")).status == JobStatus.PAUSED
