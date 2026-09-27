"""Claim writes are conditional: a stale reaper, ack or nack never overwrites a newer claim (#186).

Each worker process runs a reaper, so a reaper (or a slow worker's ack/nack) can
act on a row it read before another process released and re-claimed it. The
tests below force that interleaving by pausing a session right before it writes.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift.workers.redis as redis_backend
from skrift.db.base import Base
from skrift.db.models.worker import WorkerQueueRecord
from skrift.workers import RedisQueue, SQLAlchemyQueue
from skrift.workers.models import JobEnvelope, utcnow


@pytest.fixture
async def worker_session_maker(tmp_path):
    import skrift.db.models  # noqa: F401 - register all models on Base.metadata

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claim-races.db'}")
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


class PausingSessions:
    """Session maker whose sessions wait for ``release`` before their first write.

    Covers both a DML statement passed to ``execute`` and ORM changes flushed by
    ``commit``, so the pause lands right before the write however it is issued.
    """

    def __init__(self, session_maker):
        self._session_maker = session_maker
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self):
        session = self._session_maker()
        execute, commit = session.execute, session.commit

        async def before_write():
            if not self.release.is_set():
                self.paused.set()
                await self.release.wait()

        async def gated_execute(statement, *args, **kwargs):
            if getattr(statement, "is_dml", False):
                await before_write()
            return await execute(statement, *args, **kwargs)

        async def gated_commit():
            if session.new or session.dirty or session.deleted:
                await before_write()
            await commit()

        session.execute = gated_execute
        session.commit = gated_commit
        return session


async def _queue_row(session_maker, job_id):
    async with session_maker() as session:
        record = (
            await session.execute(
                select(WorkerQueueRecord).where(WorkerQueueRecord.job_id == job_id)
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        return record.claim_token, JobEnvelope.model_validate(record.job).reclaim_count


async def _expired_claim(queue, job):
    await queue.submit(job)
    claimed = await queue.claim(["default"], visibility_timeout=0.01)
    assert claimed is not None
    await asyncio.sleep(0.05)
    return claimed


async def _take_over(queue):
    """Another process reaps the expired claim and a new worker claims the job."""
    await queue._release_expired_claims(utcnow())
    claimed = await queue.claim(["default"], visibility_timeout=60)
    assert claimed is not None
    return claimed


async def test_stale_sqlalchemy_reaper_leaves_a_newer_claim_alone(worker_session_maker):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="race")
    await _expired_claim(queue, job)

    sessions = PausingSessions(worker_session_maker)
    reaper_1 = asyncio.create_task(
        SQLAlchemyQueue(session_maker=sessions)._release_expired_claims(utcnow())
    )
    await sessions.paused.wait()  # reaper 1 has read the expired row
    worker_b = await _take_over(queue)  # reaper 2 releases it, worker B claims it
    sessions.release.set()
    await reaper_1

    assert await _queue_row(worker_session_maker, job.id) == (worker_b.token, 1)
    assert await queue.claim(["default"], visibility_timeout=60) is None  # worker C
    await queue.ack("default", job.id, worker_b.token)


@pytest.mark.parametrize("operation", ["ack", "nack"])
async def test_sqlalchemy_ack_or_nack_racing_a_takeover_fails_without_writing(
    worker_session_maker, operation
):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="race")
    await queue.submit(job)
    worker_a = await queue.claim(["default"], visibility_timeout=0.01)
    assert worker_a is not None

    sessions = PausingSessions(worker_session_maker)
    stale_queue = SQLAlchemyQueue(session_maker=sessions)
    if operation == "ack":
        stale_write = stale_queue.ack("default", job.id, worker_a.token)
    else:
        stale_write = stale_queue.nack("default", job.id, worker_a.token, job=worker_a.job)
    stale_task = asyncio.create_task(stale_write)
    await sessions.paused.wait()  # A's token still matched when its write began
    await asyncio.sleep(0.05)
    worker_b = await _take_over(queue)
    sessions.release.set()

    with pytest.raises(ValueError, match="Invalid claim token"):
        await stale_task
    assert await _queue_row(worker_session_maker, job.id) == (worker_b.token, 1)
    assert await queue.claim(["default"], visibility_timeout=60) is None
    await queue.ack("default", job.id, worker_b.token)


@pytest.fixture(params=["sqlalchemy", "redis"])
def durable_queue(request, worker_session_maker, fake_redis_client):
    if request.param == "sqlalchemy":
        return SQLAlchemyQueue(session_maker=worker_session_maker)
    return RedisQueue(client=fake_redis_client, prefix="test:claim-races")


@pytest.mark.parametrize("operation", ["ack", "nack", "dead_letter"])
async def test_ack_or_nack_after_a_takeover_fails_and_keeps_the_new_claim(
    durable_queue, operation
):
    queue = durable_queue
    job = JobEnvelope(type="race")
    worker_a = await _expired_claim(queue, job)
    worker_b = await _take_over(queue)

    with pytest.raises(ValueError, match="Invalid claim token"):
        if operation == "ack":
            await queue.ack("default", job.id, worker_a.token)
        else:
            await queue.nack(
                "default",
                job.id,
                worker_a.token,
                dead_letter=operation == "dead_letter",
                job=worker_a.job,
            )

    stats = await queue.stats("default")
    assert (stats.claimed, stats.ready, stats.dead_lettered) == (1, 0, 0)
    assert await queue.claim(["default"], visibility_timeout=60) is None
    await queue.ack("default", job.id, worker_b.token)
    assert (await queue.stats("default")).claimed == 0


async def test_redis_reaper_holds_the_queue_lock(fake_redis_client):
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    job = JobEnvelope(type="race")
    await _expired_claim(queue, job)

    reaper_queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    read_job = reaper_queue._get_job
    paused, release = asyncio.Event(), asyncio.Event()

    async def get_job_then_pause(job_id):
        found = await read_job(job_id)
        paused.set()
        await release.wait()
        return found

    reaper_queue._get_job = get_job_then_pause
    reaper_1 = asyncio.create_task(reaper_queue._release_expired_claims(utcnow()))
    await paused.wait()  # reaper 1 has read the expired job
    # Worker B's claim reaps and claims the job unless reaper 1 holds the lock.
    worker_b_claim = asyncio.create_task(queue.claim(["default"], visibility_timeout=60))
    await asyncio.wait({worker_b_claim}, timeout=0.3)
    release.set()
    await reaper_1
    worker_b = await worker_b_claim

    assert worker_b is not None
    assert worker_b.job.reclaim_count == 1
    assert await queue.claim(["default"], visibility_timeout=60) is None  # worker C
    await queue.ack("default", job.id, worker_b.token)


async def test_redis_lease_uses_the_server_clock(fake_redis_client, monkeypatch):
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    job = JobEnvelope(type="skewed", scheduled_for=utcnow() - timedelta(hours=2))
    await queue.submit(job)

    # The claiming worker's clock runs an hour behind the rest of the fleet.
    real_now = redis_backend._now
    monkeypatch.setattr(redis_backend, "_now", lambda: real_now() - timedelta(hours=1))
    claimed = await queue.claim(["default"], visibility_timeout=60)
    monkeypatch.setattr(redis_backend, "_now", real_now)
    assert claimed is not None

    await queue._release_expired_claims(utcnow())

    assert (await queue.stats("default")).claimed == 1
    await queue.ack("default", job.id, claimed.token)
