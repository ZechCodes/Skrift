"""A dead letter whose record the dead-letter store fails to save (#227).

The job's state is already DEAD_LETTERED and its queue entry dead-lettered,
so nothing will run it again, and with no record it cannot be replayed. The
failure is logged at error level, naming the job, and still raised; no
``job_dead_lettered`` event or dead callback claims a record that does not
exist. Each path runs on the in-memory, SQLite and (fake) Redis backends.
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
from skrift.workers.models import JobStatus
from skrift.workers.registry import handler, registry
from tests.test_worker_state_writes import (
    _record_lifecycle,
    redis_backends,
    sqlalchemy_backends,
)

QUEUE = "dead-letter-record"


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


def _register(dead):
    @handler("record.fails", queue=QUEUE, max_attempts=1)
    async def fails(payload: Item, context):
        if payload.n == 0:
            raise PermanentFailure("no")
        raise RuntimeError("boom")

    @fails.on_dead
    async def on_dead(entry):
        dead.append(entry)


def _break_create(runtime):
    async def create(entry):
        raise StoreDown("dead-letter store is down")

    runtime.dead_letter_store.create = create


async def _run(backends, path):
    dead = []
    _register(dead)
    mode = "inline" if path == "inline" else "in_process"
    runtime = skrift.configure_workers(mode=mode, queues=(QUEUE,), poll_interval=0.01, **backends)
    events = _record_lifecycle(runtime)
    _break_create(runtime)
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
    records = [entry for entry in await runtime.dead_letter_store.list() if entry.job.id == job_id]
    return job_id, raised, state, records, events, dead


CAUSES = {"queued": "permanent_failure", "inline": "retries_exhausted", "poison": "poison"}


@pytest.mark.parametrize("path", list(CAUSES))
async def test_a_dead_letter_record_that_fails_to_save_is_logged_and_raised(backends, path, caplog):
    caplog.set_level(logging.INFO, logger="skrift.workers.runtime")
    job_id, raised, state, records, events, dead = await _run(backends, path)

    assert state.status == JobStatus.DEAD_LETTERED
    assert records == []
    assert "job_dead_lettered" not in [event for event, _ in events]
    assert dead == []

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    [error] = errors
    message = error.getMessage()
    for text in (job_id, QUEUE, "record.fails", CAUSES[path], "DEAD_LETTERED"):
        assert text in message
    assert "no dead-letter record" in message and "cannot be replayed" in message
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
