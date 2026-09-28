"""Job state writes ordered against state-store updates, on Postgres and a real Redis (#217).

Runs the scenarios from tests/test_worker_state_writes.py against live backends.

Requires running PostgreSQL and Redis — see compose.yml.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

import skrift
from skrift.db.models.worker import (
    WorkerDeadLetterRecord,
    WorkerQueueRecord,
    WorkerStateRecord,
)
from skrift.workers.models import JobStatus
from skrift.workers.registry import registry
from tests.test_worker_state_writes import (
    PRUNE_RACES,
    READERS,
    assert_prune_after_update,
    assert_submitted_once,
    redis_backends,
    run_a_cancel_racing_a_held_start,
    run_a_cancel_racing_an_inline_run_that_settles_first,
    run_a_prune_racing_a_held_update,
    run_a_prune_whose_lock_expires_before_its_delete,
    run_a_scheduled_wake_of_a_paused_inline_job_racing_a_wake,
    run_an_expired_get_racing_an_update,
    run_an_index_cleanup_racing_a_recreating_update,
    run_concurrent_submits_of_one_id,
    run_two_wakes_of_a_paused_inline_then_queued_job,
    sqlalchemy_backends,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_worker_registry():
    registry.clear()
    yield
    registry.clear()
    skrift.configure_workers(mode="inline")


@pytest.fixture
async def worker_pg_session_maker(pg_session_maker):
    async with pg_session_maker() as session:
        for model in (WorkerQueueRecord, WorkerStateRecord, WorkerDeadLetterRecord):
            await session.execute(delete(model))
        await session.commit()
    return pg_session_maker


@pytest.fixture
async def live_redis(redis_url):
    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(redis_url)
    prefix = f"state-writes-{uuid4().hex}"
    yield client, prefix
    keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.fixture(params=["postgres", "redis"])
def live_backends(request):
    if request.param == "postgres":
        return sqlalchemy_backends(request.getfixturevalue("worker_pg_session_maker"))
    client, prefix = request.getfixturevalue("live_redis")
    return redis_backends(client, prefix)


@pytest.mark.parametrize(
    "first,second",
    [("valid", "valid"), ("valid", "other"), ("valid", "poison"), ("poison", "poison")],
)
async def test_concurrent_submits_of_one_id_record_it_once(live_backends, first, second):
    results, state, stats, dead, recorded = await run_concurrent_submits_of_one_id(
        live_backends, first, second
    )
    assert_submitted_once(first, second, results, state, stats, dead, recorded)


async def test_a_cancel_does_not_overwrite_a_run_that_settled_first(live_backends):
    cancelled, state, events = await run_a_cancel_racing_an_inline_run_that_settles_first(
        live_backends
    )
    assert cancelled is False
    assert (state.status, state.result) == (JobStatus.COMPLETED, "done")
    assert "job_cancelled" not in [event for event, _ in events]


async def test_a_cancel_racing_a_runs_start_is_not_lost(live_backends):
    cancelled, runs, state = await run_a_cancel_racing_a_held_start(live_backends)
    assert (cancelled, runs, state.status) == (False, [1], JobStatus.COMPLETED)


async def test_two_wakes_of_a_paused_job_queue_it_once(live_backends):
    woken, runs, state = await run_two_wakes_of_a_paused_inline_then_queued_job(live_backends)
    assert (woken, runs, state.status) == ((False, True), [1], JobStatus.COMPLETED)


async def test_a_scheduled_wake_does_not_rerun_a_job_another_wake_finished(live_backends):
    woken, runs, state = await run_a_scheduled_wake_of_a_paused_inline_job_racing_a_wake(
        live_backends
    )
    assert (woken, runs, state.status) == ((False, True), [1], JobStatus.COMPLETED)


@pytest.mark.parametrize("race", list(PRUNE_RACES))
async def test_a_redis_prune_waits_for_an_update_and_rechecks_the_state(live_redis, race):
    client, prefix = live_redis
    pruned, value, counts = await run_a_prune_racing_a_held_update(client, prefix, race)
    assert_prune_after_update(race, pruned, value, counts)


async def test_a_redis_prune_that_lost_its_lock_deletes_nothing(live_redis):
    client, prefix = live_redis
    pruned, value = await run_a_prune_whose_lock_expires_before_its_delete(client, prefix)
    assert pruned == 0
    assert value is not None and value.status == JobStatus.RUNNING


@pytest.mark.parametrize("reader", READERS)
async def test_a_redis_index_cleanup_keeps_a_value_an_update_recreated(live_redis, reader):
    client, prefix = live_redis
    listed, states, counts = await run_an_index_cleanup_racing_a_recreating_update(
        client, prefix, reader
    )
    assert (listed, states, counts["total"]) == (True, ["recreated"], 1)


async def test_an_expired_get_on_postgres_does_not_delete_a_row_an_update_refreshed(
    worker_pg_session_maker,
):
    read, value = await run_an_expired_get_racing_an_update(worker_pg_session_maker)
    assert (read, value) == (None, ["fresh"])
