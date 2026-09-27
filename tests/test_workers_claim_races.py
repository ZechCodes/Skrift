"""Claim writes are conditional: a stale reaper, ack or nack never overwrites a newer claim (#186).

Each worker process runs a reaper, so a reaper (or a slow worker's ack/nack) can
act on a row it read before another process released and re-claimed it. The
tests below force that interleaving by pausing a session right before it writes.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from redis.exceptions import LockNotOwnedError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift.workers.redis as redis_backend
from skrift.db.base import Base
from skrift.db.models.worker import WorkerQueueRecord
from skrift.workers import InMemoryQueue, RedisQueue, SQLAlchemyQueue
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


async def test_sqlalchemy_claim_that_lost_a_race_does_not_overwrite_the_envelope(
    worker_session_maker,
):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="race")
    await queue.submit(job)

    sessions = PausingSessions(worker_session_maker)
    worker_a_claim = asyncio.create_task(
        SQLAlchemyQueue(session_maker=sessions).claim(["default"], visibility_timeout=60)
    )
    await sessions.paused.wait()  # A has picked the ready row
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    worker_b.job.attempt = 1
    await queue.nack(
        "default", job.id, worker_b.token, retry_at=utcnow() - timedelta(seconds=60), job=worker_b.job
    )
    sessions.release.set()
    worker_a = await worker_a_claim

    # A may still claim the job B released, but with B's attempt, not its stale copy.
    assert worker_a is not None
    assert worker_a.job.attempt == 1
    async with worker_session_maker() as session:
        stored = (
            await session.execute(select(WorkerQueueRecord.job).where(WorkerQueueRecord.job_id == job.id))
        ).scalar_one()
    assert stored["attempt"] == 1


async def test_sqlalchemy_claim_loses_to_a_concurrent_claim(worker_session_maker):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="race")
    await queue.submit(job)

    sessions = PausingSessions(worker_session_maker)
    worker_a_claim = asyncio.create_task(
        SQLAlchemyQueue(session_maker=sessions).claim(["default"], visibility_timeout=60)
    )
    await sessions.paused.wait()
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    sessions.release.set()

    assert worker_b is not None
    assert await worker_a_claim is None
    await queue.ack("default", job.id, worker_b.token)


async def test_sqlalchemy_cancel_racing_a_claim_does_not_delete_it(worker_session_maker):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="race")
    await queue.submit(job)

    sessions = PausingSessions(worker_session_maker)
    cancel = asyncio.create_task(
        SQLAlchemyQueue(session_maker=sessions).cancel("default", job.id)
    )
    await sessions.paused.wait()  # the job was unclaimed when cancel looked
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    sessions.release.set()

    assert await cancel is False
    await queue.ack("default", job.id, worker_b.token)


async def test_sqlalchemy_wake_racing_a_takeover_keeps_the_new_envelope(worker_session_maker):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    job = JobEnvelope(type="race")
    await _expired_claim(queue, job)

    sessions = PausingSessions(worker_session_maker)
    wake = asyncio.create_task(SQLAlchemyQueue(session_maker=sessions).wake("default", job.id))
    await sessions.paused.wait()  # wake read A's expired claim
    worker_b = await _take_over(queue)
    sessions.release.set()

    assert await wake is True  # recorded on B's claim for B's nack
    assert await _queue_row(worker_session_maker, job.id) == (worker_b.token, 1)


async def test_redis_release_is_conditional_after_the_queue_lock_expires(
    fake_redis_client, monkeypatch
):
    def short_lock(self):
        return self._client.lock(self._key("queue", "lock"), timeout=0.2)

    monkeypatch.setattr(RedisQueue, "_queue_lock", short_lock)
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
    await paused.wait()
    await asyncio.sleep(0.3)  # reaper 1's lock expires while it is stalled
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    release.set()
    with pytest.raises(LockNotOwnedError):
        await reaper_1

    assert worker_b.job.reclaim_count == 1
    assert await queue.claim(["default"], visibility_timeout=60) is None  # worker C
    await queue.ack("default", job.id, worker_b.token)


async def test_redis_readiness_stays_on_the_host_clock(fake_redis_client, monkeypatch):
    # The submitting and claiming host runs a minute ahead of the Redis server.
    real_now = redis_backend._now
    monkeypatch.setattr(redis_backend, "_now", lambda: real_now() + timedelta(seconds=60))
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    job = JobEnvelope(type="skewed")
    await queue.submit(job)

    assert (await queue.stats("default")).ready == 1
    claimed = await queue.claim(["default"], visibility_timeout=60)
    assert claimed is not None and claimed.job.id == job.id



class PauseBeforeFirstWrite:
    """Redis client proxy that stalls before the first write command it sends."""

    WRITES = frozenset({"set", "delete", "zadd", "zrem", "hset", "sadd", "srem", "eval"})

    def __init__(self, client):
        self._client = client
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        attribute = getattr(self._client, name)
        if name not in self.WRITES:
            return attribute

        async def gated(*args, **kwargs):
            if not self.release.is_set():
                self.paused.set()
                await self.release.wait()
            return await attribute(*args, **kwargs)

        return gated


@pytest.fixture
def short_redis_lock(monkeypatch):
    """A 0.2 s queue lock, so a stalled operation outlives it quickly."""

    def short_lock(self):
        return self._client.lock(self._key("queue", "lock"), timeout=0.2)

    monkeypatch.setattr(RedisQueue, "_queue_lock", short_lock)


async def _outlive_the_lock_then_claim(stale, operation, queue):
    """Run ``operation`` on the stale queue until its first write, let its lock
    expire, and have worker B claim the job meanwhile."""
    stale_task = asyncio.create_task(operation)
    await stale._client.paused.wait()
    await asyncio.sleep(0.3)
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    stale._client.release.set()
    with pytest.raises(LockNotOwnedError):
        await stale_task
    return worker_b


@pytest.mark.parametrize("operation", ["claim", "cancel", "wake"])
async def test_redis_write_after_the_lock_expires_leaves_a_new_claim(
    fake_redis_client, short_redis_lock, operation
):
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    stale = RedisQueue(client=PauseBeforeFirstWrite(fake_redis_client), prefix="test:claim-races")
    job = JobEnvelope(type="race")
    await queue.submit(job)
    if operation == "claim":
        stale_operation = stale.claim(["default"], visibility_timeout=60)
    elif operation == "cancel":
        stale_operation = stale.cancel("default", job.id)
    else:
        stale_operation = stale.wake("default", job.id)

    worker_b = await _outlive_the_lock_then_claim(stale, stale_operation, queue)

    assert await fake_redis_client.hget(queue._claim_key(job.id), "token") == worker_b.token.encode()
    assert await queue.claim(["default"], visibility_timeout=60) is None
    await queue.ack("default", job.id, worker_b.token)


async def test_redis_missing_job_cleanup_keeps_a_resubmitted_job(
    fake_redis_client, short_redis_lock
):
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    reaper_queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    job = JobEnvelope(type="race")
    worker_a = await _expired_claim(queue, job)

    read_job = reaper_queue._get_job
    before_read, read_allowed = asyncio.Event(), asyncio.Event()
    after_read, finish_allowed = asyncio.Event(), asyncio.Event()

    async def gated_read(job_id):
        before_read.set()
        await read_allowed.wait()
        found = await read_job(job_id)
        after_read.set()
        await finish_allowed.wait()
        return found

    reaper_queue._get_job = gated_read
    reaper_1 = asyncio.create_task(reaper_queue._release_expired_claims(utcnow()))
    await before_read.wait()  # reaper 1 listed A's expired claim
    await asyncio.sleep(0.3)
    await queue.ack("default", job.id, worker_a.token)  # A finishes late
    read_allowed.set()
    await after_read.wait()  # reaper 1 found the job gone
    await queue.submit(JobEnvelope(id=job.id, type="race"))
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    finish_allowed.set()
    with pytest.raises(LockNotOwnedError):
        await reaper_1

    # B's lease is still indexed, so the reaper can reclaim it if B dies.
    assert await fake_redis_client.zscore(queue._claimed_key("default"), job.id) is not None
    await queue.ack("default", job.id, worker_b.token)


async def test_sqlalchemy_wake_racing_a_claim_and_retry_keeps_the_new_envelope(
    worker_session_maker,
):
    queue = SQLAlchemyQueue(session_maker=worker_session_maker)
    visible_at = utcnow() - timedelta(seconds=60)
    job = JobEnvelope(type="race", scheduled_for=visible_at)
    await queue.submit(job)

    sessions = PausingSessions(worker_session_maker)
    wake = asyncio.create_task(SQLAlchemyQueue(session_maker=sessions).wake("default", job.id))
    await sessions.paused.wait()
    # B runs a whole claim/retry cycle that ends at the same visible_at.
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    worker_b.job.attempt = 1
    await queue.nack("default", job.id, worker_b.token, retry_at=visible_at, job=worker_b.job)
    sessions.release.set()

    assert await wake is True
    assert (await _stored_job(worker_session_maker, job.id)).attempt == 1


def _queue_for(backend, worker_session_maker, fake_redis_client):
    if backend == "memory":
        return InMemoryQueue()
    if backend == "sqlalchemy":
        return SQLAlchemyQueue(session_maker=worker_session_maker)
    return RedisQueue(client=fake_redis_client, prefix="test:claim-races")


LATER = timedelta(hours=2)


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
@pytest.mark.parametrize(
    ("wakes", "ready_now"),
    [
        (["now"], True),
        (["later", "now"], True),  # the latest wake wins
        (["now", "later"], False),
    ],
)
async def test_wake_of_a_claimed_job_is_applied_by_its_nack(
    worker_session_maker, fake_redis_client, backend, wakes, ready_now
):
    queue = _queue_for(backend, worker_session_maker, fake_redis_client)
    job = JobEnvelope(type="race")
    await queue.submit(job)
    worker_a = await queue.claim(["default"], visibility_timeout=60)

    for wake in wakes:
        resume_at = None if wake == "now" else utcnow() + LATER
        assert await queue.wake("default", job.id, resume_at=resume_at) is True
    # The claim is untouched until the worker settles it.
    assert await queue.claim(["default"], visibility_timeout=60) is None
    await queue.nack("default", job.id, worker_a.token, retry_at=utcnow() + timedelta(days=30), job=worker_a.job)

    resumed = await queue.claim(["default"], visibility_timeout=60)
    assert (resumed is not None) is ready_now
    stats = await queue.stats("default")
    assert (stats.claimed, stats.delayed) == ((1, 0) if ready_now else (0, 1))


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
@pytest.mark.parametrize("outcome", ["ack", "dead_letter", "reaped"])
async def test_wake_of_a_claimed_job_is_dropped_when_the_claim_ends_otherwise(
    worker_session_maker, fake_redis_client, backend, outcome
):
    queue = _queue_for(backend, worker_session_maker, fake_redis_client)
    job = JobEnvelope(type="race")
    await queue.submit(job)
    worker_a = await queue.claim(["default"], visibility_timeout=0.05 if outcome == "reaped" else 60)
    assert await queue.wake("default", job.id, resume_at=utcnow() + LATER) is True

    if outcome == "ack":
        await queue.ack("default", job.id, worker_a.token)
        assert await queue.claim(["default"], visibility_timeout=60) is None
        return
    if outcome == "dead_letter":
        await queue.nack("default", job.id, worker_a.token, dead_letter=True, job=worker_a.job)
        assert (await queue.stats("default")).dead_lettered == 1
        return
    await asyncio.sleep(0.1)
    await queue._release_expired_claims(utcnow())
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None  # released now, not at the dropped wake's time
    # And B's own nack is not redirected by A's old wake.
    await queue.nack("default", job.id, worker_b.token, job=worker_b.job)
    assert await queue.claim(["default"], visibility_timeout=60) is not None


@pytest.mark.parametrize("same_payload", [True, False])
async def test_redis_submit_after_the_lock_expires_keeps_the_newer_job(
    fake_redis_client, short_redis_lock, same_payload
):
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    stale = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    first = JobEnvelope(type="race", payload={"value": "A"})
    read_job = stale._get_job
    paused, release = asyncio.Event(), asyncio.Event()

    async def read_then_pause(job_id):
        found = await read_job(job_id)
        paused.set()
        await release.wait()
        return found

    stale._get_job = read_then_pause
    stale_submit = asyncio.create_task(stale.submit(first))
    await paused.wait()  # the id was free when A looked
    await asyncio.sleep(0.3)
    newer = JobEnvelope(id=first.id, type="race", payload={"value": "A" if same_payload else "B"})
    await queue.submit(newer)
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    worker_b.job.attempt = 1
    await queue.nack("default", newer.id, worker_b.token, job=worker_b.job)
    release.set()
    with pytest.raises(LockNotOwnedError):
        await stale_submit

    stored = await queue._get_job(newer.id)
    assert (stored.payload, stored.attempt) == (newer.payload, 1)


async def test_redis_prune_after_the_lock_expires_keeps_a_resubmitted_job(
    fake_redis_client, short_redis_lock
):
    queue = RedisQueue(client=fake_redis_client, prefix="test:claim-races")
    stale = RedisQueue(client=PauseBeforeFirstWrite(fake_redis_client), prefix="test:claim-races")
    job = JobEnvelope(type="race")
    await queue.submit(job)
    worker_a = await queue.claim(["default"], visibility_timeout=60)
    await queue.nack("default", job.id, worker_a.token, dead_letter=True, job=worker_a.job)

    stale_prune = asyncio.create_task(stale.prune_dead_markers(max_age_seconds=0))
    await stale._client.paused.wait()  # the pruner listed the dead job
    await asyncio.sleep(0.3)
    assert await queue.cancel("default", job.id)
    await queue.submit(JobEnvelope(id=job.id, type="race", payload={"generation": 2}))
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    stale._client.release.set()
    with pytest.raises(LockNotOwnedError):
        await stale_prune

    assert (await queue._get_job(job.id)).payload == {"generation": 2}
    await queue.ack("default", job.id, worker_b.token)


async def _stored_job(session_maker, job_id):
    async with session_maker() as session:
        stored = (
            await session.execute(select(WorkerQueueRecord.job).where(WorkerQueueRecord.job_id == job_id))
        ).scalar_one()
    return JobEnvelope.model_validate(stored)
