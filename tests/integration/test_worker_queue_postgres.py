"""SQLAlchemyQueue claim races on Postgres, where reapers run concurrently (#186).

Also the SQLAlchemy state store's concurrent updates on Postgres (#195).

Requires running PostgreSQL — see compose.yml.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

import skrift.workers.sqlalchemy as sqlalchemy_backend
from skrift.db.models.worker import WorkerQueueRecord, WorkerStateRecord
from skrift.workers import SQLAlchemyQueue, SQLAlchemyStateStore
from skrift.workers.models import JobEnvelope, utcnow
from tests.test_worker_backend_contracts import _assert_concurrent_updates_both_land
from tests.test_workers_claim_races import PausingSessions, _expired_claim, _queue_row

pytestmark = pytest.mark.integration


@pytest.fixture
async def queue_session_maker(pg_session_maker):
    async with pg_session_maker() as session:
        await session.execute(delete(WorkerQueueRecord))
        await session.commit()
    return pg_session_maker


async def test_concurrent_reapers_release_an_expired_claim_once(queue_session_maker):
    queue = SQLAlchemyQueue(session_maker=queue_session_maker)
    job = JobEnvelope(type="race")
    await _expired_claim(queue, job)

    sessions = PausingSessions(queue_session_maker)
    reaper_1 = asyncio.create_task(
        SQLAlchemyQueue(session_maker=sessions)._release_expired_claims(utcnow())
    )
    await sessions.paused.wait()  # reaper 1 holds the row it read
    # Reaper 2 skips the row reaper 1 has locked instead of waiting for it, so
    # worker B finds nothing to claim yet.
    await asyncio.wait_for(queue._release_expired_claims(utcnow()), timeout=5)
    assert await queue.claim(["default"], visibility_timeout=60) is None
    sessions.release.set()
    await reaper_1

    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    assert worker_b.job.reclaim_count == 1
    assert await queue.claim(["default"], visibility_timeout=60) is None  # worker C
    await queue.ack("default", job.id, worker_b.token)


@pytest.mark.parametrize("operation", ["ack", "nack"])
async def test_ack_or_nack_racing_a_takeover_fails_without_writing(
    queue_session_maker, operation
):
    queue = SQLAlchemyQueue(session_maker=queue_session_maker)
    job = JobEnvelope(type="race")
    await queue.submit(job)
    worker_a = await queue.claim(["default"], visibility_timeout=0.01)
    assert worker_a is not None

    sessions = PausingSessions(queue_session_maker)
    stale_queue = SQLAlchemyQueue(session_maker=sessions)
    if operation == "ack":
        stale_write = stale_queue.ack("default", job.id, worker_a.token)
    else:
        stale_write = stale_queue.nack("default", job.id, worker_a.token, job=worker_a.job)
    stale_task = asyncio.create_task(stale_write)
    await sessions.paused.wait()
    await asyncio.sleep(0.05)
    await queue._release_expired_claims(utcnow())
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    assert worker_b is not None
    sessions.release.set()

    with pytest.raises(ValueError, match="Invalid claim token"):
        await stale_task
    assert await _queue_row(queue_session_maker, job.id) == (worker_b.token, 1)
    await queue.ack("default", job.id, worker_b.token)


async def test_lease_uses_the_database_clock(queue_session_maker, monkeypatch):
    queue = SQLAlchemyQueue(session_maker=queue_session_maker)
    job = JobEnvelope(type="skewed", scheduled_for=utcnow() - timedelta(hours=2))
    await queue.submit(job)

    # The claiming worker's clock runs an hour behind the database's.
    real_now = sqlalchemy_backend._now
    monkeypatch.setattr(sqlalchemy_backend, "_now", lambda: real_now() - timedelta(hours=1))
    claimed = await queue.claim(["default"], visibility_timeout=60)
    monkeypatch.setattr(sqlalchemy_backend, "_now", real_now)
    assert claimed is not None

    await queue._release_expired_claims(utcnow())

    assert (await queue.stats("default")).claimed == 1
    await queue.ack("default", job.id, claimed.token)


async def test_readiness_stays_on_the_host_clock(queue_session_maker, monkeypatch):
    # The submitting and claiming host runs a minute ahead of the database.
    real_now = sqlalchemy_backend._now
    monkeypatch.setattr(sqlalchemy_backend, "_now", lambda: real_now() + timedelta(seconds=60))
    queue = SQLAlchemyQueue(session_maker=queue_session_maker)
    job = JobEnvelope(type="skewed")
    await queue.submit(job)

    assert (await queue.stats("default")).ready == 1
    claimed = await queue.claim(["default"], visibility_timeout=60)
    assert claimed is not None and claimed.job.id == job.id
    await queue.ack("default", job.id, claimed.token)


async def test_concurrent_claims_leave_one_owner(queue_session_maker):
    queue = SQLAlchemyQueue(session_maker=queue_session_maker)
    job = JobEnvelope(type="race")
    await queue.submit(job)

    sessions = PausingSessions(queue_session_maker)
    worker_a_claim = asyncio.create_task(
        SQLAlchemyQueue(session_maker=sessions).claim(["default"], visibility_timeout=60)
    )
    await sessions.paused.wait()  # A holds the row lock
    worker_b = await asyncio.wait_for(queue.claim(["default"], visibility_timeout=60), timeout=5)
    sessions.release.set()
    worker_a = await worker_a_claim

    assert (worker_a is None) != (worker_b is None)
    winner = worker_a or worker_b
    await queue.ack("default", job.id, winner.token)


async def test_wake_racing_a_claim_and_retry_keeps_the_new_envelope(queue_session_maker):
    queue = SQLAlchemyQueue(session_maker=queue_session_maker)
    visible_at = utcnow() - timedelta(seconds=60)
    job = JobEnvelope(type="race", scheduled_for=visible_at)
    await queue.submit(job)

    sessions = PausingSessions(queue_session_maker)
    wake = asyncio.create_task(SQLAlchemyQueue(session_maker=sessions).wake("default", job.id))
    await sessions.paused.wait()
    worker_b = await queue.claim(["default"], visibility_timeout=60)
    worker_b.job.attempt = 1
    await queue.nack("default", job.id, worker_b.token, retry_at=visible_at, job=worker_b.job)
    sessions.release.set()

    assert await wake is True
    async with queue_session_maker() as session:
        stored = (
            await session.execute(select(WorkerQueueRecord.job).where(WorkerQueueRecord.job_id == job.id))
        ).scalar_one()
    assert stored["attempt"] == 1


async def test_concurrent_state_updates_both_land(pg_session_maker):
    async with pg_session_maker() as session:
        await session.execute(delete(WorkerStateRecord))
        await session.commit()

    store = SQLAlchemyStateStore(session_maker=pg_session_maker)
    await _assert_concurrent_updates_both_land(store, existing=True)
