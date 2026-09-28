"""An inline run does not start once its job is cancelled or finished (#220).

An inline job has no queue entry for ``cancel`` to remove, so a cancel that
lands after the job's SUBMITTED state is written but before its run starts only
writes CANCELLED. The run used to start anyway, overwrite CANCELLED with
RUNNING and settle the job. Each test holds the inline run at the point where
the other side used to slip in, on the in-memory, SQLite and (fake) Redis
backends.
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
    JobCancelled,
    Pause,
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
)
from skrift.workers.models import JobStatus
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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inline_start.db'}")
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
    prefix = "test:inline-start"
    return {
        "state_store": RedisStateStore(client=fake_redis_client, prefix=prefix),
        "event_log": RedisEventLog(client=fake_redis_client, prefix=prefix),
        "queue": RedisQueue(client=fake_redis_client, prefix=prefix),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


def _hold_after(runtime, event_name):
    """Hold the run that emits ``event_name`` (once) until ``release`` is set."""
    emit = runtime.emit_lifecycle
    reached, release = asyncio.Event(), asyncio.Event()

    async def held(event_type, job, **kwargs):
        await emit(event_type, job, **kwargs)
        if event_type.value == event_name and not reached.is_set():
            reached.set()
            await release.wait()

    runtime.emit_lifecycle = held
    return reached, release


async def _events(runtime, job_id):
    return [
        event["type"]
        for _, event in await runtime.event_log.read(LIFECYCLE_STREAM)
        if event["job_id"] == job_id
    ]


@pytest.mark.parametrize("submission", ["submit", "submit_inline"])
async def test_a_job_cancelled_before_its_inline_run_starts_does_not_run(
    backends, submission
):
    runs = []

    @handler("inline.start", queue="inline-start")
    async def write(item: Item):
        runs.append(item.n)
        return "done"

    runtime = skrift.configure_workers(mode="inline", **backends)
    reached, release = _hold_after(runtime, "job_submitted")
    submit = getattr(runtime, submission)
    submitting = asyncio.create_task(submit("inline.start", Item(n=1), job_id="job"))
    await reached.wait()  # SUBMITTED is written, the run has not started

    assert await runtime.cancel("job") is True
    release.set()
    await submitting

    state = await runtime.get_job_state("job")
    assert (runs, state.status) == ([], JobStatus.CANCELLED)
    with pytest.raises(JobCancelled):
        await runtime.wait_for_result("job", timeout=1)
    assert await _events(runtime, "job") == ["job_submitted", "job_cancelled"]


async def test_a_job_cancelled_before_its_inline_retry_does_not_run_again(backends):
    runs = []

    @handler("inline.retry", queue="inline-start")
    async def fail_once(item: Item):
        runs.append(item.n)
        if len(runs) == 1:
            raise RuntimeError("first attempt fails")
        return "done"

    runtime = skrift.configure_workers(mode="inline", **backends)
    reached, release = _hold_after(runtime, "job_failed")
    submitting = asyncio.create_task(
        runtime.submit_inline("inline.retry", Item(n=1), job_id="job")
    )
    await reached.wait()  # the retry's SUBMITTED is written, its run has not started

    assert await runtime.cancel("job") is True
    release.set()
    await submitting

    state = await runtime.get_job_state("job")
    assert (runs, state.status, state.attempt) == ([1], JobStatus.CANCELLED, 1)
    assert "job_started" not in (await _events(runtime, "job"))[-2:]


async def test_an_inline_wake_does_not_rerun_a_job_another_wake_finished(backends):
    runs = []

    @handler("inline.wake", queue="inline-start")
    async def pause_once(item: Item):
        runs.append(item.n)
        return Pause() if len(runs) == 1 else f"done {len(runs)}"

    runtime = skrift.configure_workers(mode="inline", **backends)
    await runtime.submit_inline(
        "inline.wake", Item(n=1), job_id="job", metadata={"skrift_dispatch": "inline"}
    )
    assert (await runtime.get_job_state("job")).status == JobStatus.PAUSED

    get_job_state = runtime.get_job_state
    read, release = asyncio.Event(), asyncio.Event()

    async def hold_first_read(job_id):
        state = await get_job_state(job_id)
        if not read.is_set():
            read.set()
            await release.wait()
        return state

    runtime.get_job_state = hold_first_read
    late = asyncio.create_task(runtime.wake("job"))
    await read.wait()  # the late wake read PAUSED; its run has not started
    runtime.get_job_state = get_job_state

    assert await runtime.wake("job") is True
    release.set()
    await late

    state = await runtime.get_job_state("job")
    assert (runs, state.status, state.result) == ([1, 1], JobStatus.COMPLETED, "done 2")
