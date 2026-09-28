"""A claim lasts at least the job's own visibility timeout (#142).

``@handler(visibility_timeout=...)`` is stored on the job's envelope, but worker
loops claim with the global ``workers.visibility_timeout``. The queue sets the
lease when it hands out the claim, so it holds the claim for the longer of the
two and a long handler is not reclaimed mid-run by another worker. A job that
sets no timeout of its own gets exactly the worker's.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from skrift.db.base import Base
from skrift.workers import (
    HandlerRegistry,
    InMemoryQueue,
    RedisQueue,
    SQLAlchemyQueue,
    WorkerConfig,
    WorkerRuntime,
)
from skrift.workers.models import JobEnvelope, JobStatus, utcnow
from skrift.workers.redis import _job_to_json


@pytest.fixture(params=["memory", "sqlalchemy", "redis"])
async def queue(request, tmp_path):
    if request.param == "memory":
        yield InMemoryQueue()
    elif request.param == "sqlalchemy":
        from skrift.db import models as _models  # noqa: F401 - register all models

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'vt.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            yield SQLAlchemyQueue(
                session_maker=async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
            )
        finally:
            await engine.dispose()
    else:
        import fakeredis.aioredis as fake_aioredis

        client = fake_aioredis.FakeRedis()
        await client.flushall()
        try:
            yield RedisQueue(client=client, prefix="test:job-vt")
        finally:
            await client.aclose()


async def _claim_then_let_the_worker_lease_lapse(queue, *, job_vt: float, claim_vt: float):
    await queue.submit(JobEnvelope(type="long", visibility_timeout=job_vt))
    return await _claim_then_let_the_worker_lease_lapse_on(queue, claim_vt=claim_vt)


async def _claim_then_let_the_worker_lease_lapse_on(queue, *, claim_vt: float):
    claimed = await queue.claim(["default"], visibility_timeout=claim_vt)
    assert claimed is not None
    await asyncio.sleep(0.1)
    await queue._release_expired_claims(utcnow())
    return claimed


async def test_claim_is_held_for_the_jobs_longer_visibility_timeout(queue):
    claimed = await _claim_then_let_the_worker_lease_lapse(queue, job_vt=60, claim_vt=0.01)

    assert await queue.claim(["default"], visibility_timeout=60) is None
    await queue.ack("default", claimed.job.id, claimed.token)


async def test_claim_is_held_for_the_workers_longer_visibility_timeout(queue):
    # A job whose own timeout is shorter still gets the worker's lease.
    claimed = await _claim_then_let_the_worker_lease_lapse(queue, job_vt=0.01, claim_vt=60)

    assert await queue.claim(["default"], visibility_timeout=60) is None
    await queue.ack("default", claimed.job.id, claimed.token)


async def test_a_job_without_its_own_timeout_gets_the_workers_lease(queue):
    # Even a worker lease shorter than the old 30 s job default.
    await queue.submit(JobEnvelope(type="short"))
    await _claim_then_let_the_worker_lease_lapse_on(queue, claim_vt=0.01)

    assert await queue.claim(["default"], visibility_timeout=60) is not None


async def test_claim_expires_once_both_visibility_timeouts_pass(queue):
    await _claim_then_let_the_worker_lease_lapse(queue, job_vt=0.01, claim_vt=0.01)

    assert await queue.claim(["default"], visibility_timeout=60) is not None


@pytest.mark.parametrize("queue", ["redis"], indirect=True)
async def test_redis_claim_skips_a_job_replaced_after_its_timeout_was_read(queue):
    # The Redis lease is computed from the envelope read before the claim script.
    job = JobEnvelope(type="long", visibility_timeout=0.01)
    await queue.submit(job)
    read_job = queue._get_job

    async def read_then_replace(job_id):
        read = await read_job(job_id)
        # The job is cancelled and resubmitted under the same id with a longer
        # timeout between the read and the claim script.
        replaced = job.model_copy(update={"visibility_timeout": 60.0})
        await queue._client.set(queue._job_key(job_id), _job_to_json(replaced))
        return read

    queue._get_job = read_then_replace
    assert await queue.claim(["default"], visibility_timeout=0.01) is None
    queue._get_job = read_job

    claimed = await _claim_then_let_the_worker_lease_lapse_on(queue, claim_vt=0.01)
    assert claimed.job.visibility_timeout == 60.0
    assert await queue.claim(["default"], visibility_timeout=60) is None


@pytest.mark.parametrize("queue", ["redis"], indirect=True)
async def test_redis_claim_skips_a_job_given_a_timeout_after_it_was_read(queue):
    job = JobEnvelope(type="long")
    await queue.submit(job)
    read_job = queue._get_job

    async def read_then_replace(job_id):
        read = await read_job(job_id)
        replaced = job.model_copy(update={"visibility_timeout": 60.0})
        await queue._client.set(queue._job_key(job_id), _job_to_json(replaced))
        return read

    queue._get_job = read_then_replace
    assert await queue.claim(["default"], visibility_timeout=0.01) is None
    queue._get_job = read_job

    claimed = await _claim_then_let_the_worker_lease_lapse_on(queue, claim_vt=0.01)
    assert claimed.job.visibility_timeout == 60.0
    assert await queue.claim(["default"], visibility_timeout=60) is None


class Long(BaseModel):
    pass


async def test_a_handler_outliving_the_global_timeout_runs_once():
    registry = HandlerRegistry()
    runs = 0
    first_run_done = asyncio.Event()

    async def long_handler(payload: Long) -> str:
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.5)
        first_run_done.set()
        return "done"

    registry.register("tests.long", long_handler, payload_model=Long, visibility_timeout=60)
    runtime = WorkerRuntime(
        config=WorkerConfig(
            mode="in_process",
            concurrency=2,
            poll_interval=0.01,
            visibility_timeout=0.05,
            reaper_interval=0.02,
        ),
        handler_registry=registry,
    )
    await runtime.start()
    try:
        handle = await runtime.submit("tests.long", {})
        await asyncio.wait_for(first_run_done.wait(), 5)
        await asyncio.sleep(0.1)  # let the worker record the outcome
        state = await runtime.get_job_state(handle.id)
    finally:
        await runtime.stop()

    assert runs == 1
    assert state.status == JobStatus.COMPLETED


async def test_a_job_submitted_without_a_timeout_stores_none():
    # None on the envelope means "use the worker's", so a handler that sets no
    # timeout no longer pins its jobs to 30 s.
    registry = HandlerRegistry()
    registry.register("tests.plain", lambda payload: None, payload_model=Long)
    registry.register(
        "tests.timed", lambda payload: None, payload_model=Long, visibility_timeout=45
    )
    runtime = WorkerRuntime(config=WorkerConfig(mode="in_process"), handler_registry=registry)

    plain = await runtime.submit("tests.plain", {})
    overridden = await runtime.submit("tests.plain", {}, visibility_timeout=5)
    timed = await runtime.submit("tests.timed", {})

    assert registry.get("tests.plain").visibility_timeout is None
    assert (await runtime.get_job_state(plain.id)).job.visibility_timeout is None
    assert (await runtime.get_job_state(overridden.id)).job.visibility_timeout == 5
    assert (await runtime.get_job_state(timed.id)).job.visibility_timeout == 45


async def test_a_claim_reports_the_lease_it_is_held_for(queue):
    await queue.submit(JobEnvelope(type="long", visibility_timeout=60))
    await queue.submit(JobEnvelope(type="short"))

    longer = await queue.claim(["default"], visibility_timeout=0.05)
    workers = await queue.claim(["default"], visibility_timeout=0.05)

    assert (longer.visibility_timeout, workers.visibility_timeout) == (60, 0.05)


async def test_a_worker_runs_a_claim_held_past_its_own_timeout(queue):
    # The worker's lease has passed, but the job's longer one has not: the
    # claim is still this worker's, so it must not be skipped as expired.
    registry = HandlerRegistry()
    runs = []
    registry.register(
        "tests.long", lambda payload: runs.append(payload) or "done", payload_model=Long,
        visibility_timeout=60,
    )
    runtime = WorkerRuntime(
        config=WorkerConfig(mode="in_process", visibility_timeout=0.05),
        handler_registry=registry,
        queue=queue,
    )
    handle = await runtime.submit("tests.long", {})
    claimed = await queue.claim(["default"], visibility_timeout=0.05)
    await asyncio.sleep(0.1)

    await asyncio.wait_for(runtime.execute_claim(claimed), 5)

    assert len(runs) == 1
    assert (await runtime.get_job_state(handle.id)).status == JobStatus.COMPLETED
