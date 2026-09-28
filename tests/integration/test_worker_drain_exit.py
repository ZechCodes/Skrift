"""A worker process's abandoned run settles nothing, on Postgres and Redis (#184).

Requires running PostgreSQL and Redis — see compose.yml.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import skrift
from skrift.workers import (
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
)
from skrift.workers.registry import registry
from tests.test_workers_drain import run_a_worker_process_whose_abandoned_run_ends_during_cleanup

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_worker_registry():
    registry.clear()
    yield
    registry.clear()
    skrift.configure_workers(mode="inline")


async def test_a_worker_process_on_postgres_settles_nothing_for_a_run_it_abandoned(
    tmp_path, pg_url, pg_session_maker
):
    backends = {
        "state_store": SQLAlchemyStateStore(session_maker=pg_session_maker),
        "event_log": SQLAlchemyEventLog(session_maker=pg_session_maker),
        "queue": SQLAlchemyQueue(session_maker=pg_session_maker),
        "dead_letter_store": SQLAlchemyDeadLetterStore(session_maker=pg_session_maker),
    }
    await run_a_worker_process_whose_abandoned_run_ends_during_cleanup(
        tmp_path, backends, db_url=pg_url, queue=f"drain-{uuid4().hex}"
    )


async def test_a_worker_process_on_redis_settles_nothing_for_a_run_it_abandoned(
    tmp_path, pg_url, redis_url, pg_session_maker
):
    import redis.asyncio as aioredis

    prefix = f"drain-{uuid4().hex}"
    client = aioredis.Redis.from_url(redis_url)
    workers_prefix = f"{prefix}:skrift:workers"
    backends = {
        "state_store": RedisStateStore(client=client, prefix=workers_prefix),
        "event_log": RedisEventLog(client=client, prefix=workers_prefix),
        "queue": RedisQueue(client=client, prefix=workers_prefix),
        "dead_letter_store": SQLAlchemyDeadLetterStore(session_maker=pg_session_maker),
    }
    try:
        await run_a_worker_process_whose_abandoned_run_ends_during_cleanup(
            tmp_path,
            backends,
            db_url=pg_url,
            preset="distributed",
            redis={"url": redis_url, "prefix": prefix},
        )
    finally:
        async for key in client.scan_iter(f"{prefix}:*"):
            await client.delete(key)
        await client.aclose()
