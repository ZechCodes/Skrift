"""Stopping a worker pool drains it instead of cancelling its jobs outright (#184).

A stop request stops the pool claiming. Jobs already running get
``drain_timeout`` to finish and settle as usual. Those still running are then
cancelled and their claims released at once, charging no attempt, so another
worker takes them straight away. A handler that ignores its cancellation for
``drain_cancel_timeout`` more is left behind, its claim to expire, so stopping
has an upper bound; the runtime's own writes for a job are never cut short.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.db.base import Base
from skrift.workers import (
    InMemoryDeadLetterStore,
    InMemoryEventLog,
    InMemoryQueue,
    InMemoryStateStore,
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
from skrift.workers.registry import registry

BACKENDS = ["memory", "sqlalchemy", "redis"]


class Work(BaseModel):
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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drain.db'}")
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


@pytest.fixture
def backends(request, worker_session_maker, fake_redis_client):
    """Backends shared by a worker and its successor, as a deploy's two pods share them."""

    if request.param == "memory":
        return {
            "state_store": InMemoryStateStore(),
            "event_log": InMemoryEventLog(),
            "queue": InMemoryQueue(),
            "dead_letter_store": InMemoryDeadLetterStore(),
        }
    if request.param == "sqlalchemy":
        return {
            "state_store": SQLAlchemyStateStore(session_maker=worker_session_maker),
            "event_log": SQLAlchemyEventLog(session_maker=worker_session_maker),
            "queue": SQLAlchemyQueue(session_maker=worker_session_maker),
            "dead_letter_store": SQLAlchemyDeadLetterStore(session_maker=worker_session_maker),
        }
    return {
        "state_store": RedisStateStore(client=fake_redis_client, prefix="test:drain"),
        "event_log": RedisEventLog(client=fake_redis_client, prefix="test:drain"),
        "queue": RedisQueue(client=fake_redis_client, prefix="test:drain"),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


def _worker(backends, **config):
    config.setdefault("visibility_timeout", 30)
    config.setdefault("poll_interval", 0.01)
    config.setdefault("max_poll_interval", 0.02)
    return skrift.configure_workers(mode="in_process", **backends, **config)


class Handler:
    """A handler whose runs are logged and, unless ``seconds`` says otherwise, wait
    for ``release``."""

    def __init__(self, *, seconds=None, on_cancel="raise"):
        self.seconds = seconds
        self.on_cancel = on_cancel
        self.log: list[tuple[str, int]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, job: Work):
        self.log.append(("start", job.n))
        self.started.set()
        try:
            if self.seconds is None:
                await self.release.wait()
            else:
                await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.log.append(("cancelled", job.n))
            if self.on_cancel == "ignore":
                await self.release.wait()
            elif self.on_cancel == "convert":
                raise RuntimeError("cancelled") from None
            else:
                raise
        self.log.append(("finished", job.n))
        return job.n


def _register(handler, **options):
    @skrift.handler("drain.work", **options)
    async def work(job: Work):
        return await handler(job)


async def _stop(runtime):
    started = time.monotonic()
    await asyncio.wait_for(runtime.stop(), 5)
    return time.monotonic() - started


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_job_that_finishes_inside_the_drain_window_completes(backends):
    handler = Handler(seconds=0.3)
    _register(handler)
    runtime = _worker(backends, drain_timeout=5)
    await runtime.start()
    handle = await runtime.submit(Work(n=1))
    await handler.started.wait()

    await _stop(runtime)

    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, 1)
    assert handler.log == [("start", 1), ("finished", 1)]
    assert await backends["queue"].claim(["default"], visibility_timeout=30) is None


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_job_running_past_the_drain_window_goes_straight_to_a_successor(backends):
    # The issue's repro: a 30 s claim on a job cut short by a deploy.
    handler = Handler(seconds=3)
    _register(handler, max_attempts=1)
    runtime = _worker(backends, drain_timeout=0.2)
    await runtime.start()
    handle = await runtime.submit(Work(n=1))
    await handler.started.wait()

    assert await _stop(runtime) < 2
    state = await handle.status()
    assert state.status == JobStatus.SUBMITTED
    assert state.attempt == 0  # the drain charged no attempt: max_attempts=1 still runs it

    handler.started.clear()
    handler.seconds = 0
    successor = _worker(backends)
    await successor.start()
    try:
        await asyncio.wait_for(successor.handle(handle.id).result(), 2)
    finally:
        await asyncio.wait_for(successor.stop(), 5)
    assert handler.log == [("start", 1), ("cancelled", 1), ("start", 1), ("finished", 1)]
    state = await handle.status()
    assert (state.status, state.attempt, state.job.reclaim_count) == (JobStatus.COMPLETED, 1, 0)


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_draining_pool_claims_no_new_jobs(backends):
    handler = Handler()
    _register(handler)
    runtime = _worker(backends, concurrency=2, drain_timeout=5)
    await runtime.start()
    first = await runtime.submit(Work(n=1))
    await handler.started.wait()

    stopping = asyncio.create_task(_stop(runtime))
    await asyncio.sleep(0.05)
    second = await runtime.submit(Work(n=2))
    await asyncio.sleep(0.2)  # the idle worker polls many times over
    handler.release.set()
    await stopping

    assert (await first.status()).status == JobStatus.COMPLETED
    assert (await second.status()).status == JobStatus.SUBMITTED
    assert handler.log == [("start", 1), ("finished", 1)]


async def test_an_idle_pool_stops_without_waiting_out_its_poll_interval():
    runtime = _worker({}, concurrency=3, poll_interval=5, max_poll_interval=5)
    await runtime.start()
    await asyncio.sleep(0.05)  # every worker is between polls

    assert await _stop(runtime) < 1


async def test_a_worker_stuck_claiming_does_not_hold_up_the_stop():
    queue = InMemoryQueue()
    claiming = asyncio.Event()

    async def stuck_claim(*args, **kwargs):
        claiming.set()
        await asyncio.Event().wait()

    queue.claim = stuck_claim
    runtime = _worker({"queue": queue}, drain_timeout=0.1, drain_cancel_timeout=0.1)
    await runtime.start()
    await claiming.wait()

    assert await _stop(runtime) < 1


async def test_a_job_reaching_its_handler_after_the_drain_window_is_handed_back_unrun():
    backends = {"queue": InMemoryQueue(), "state_store": InMemoryStateStore()}
    handler = Handler()
    _register(handler)
    store = backends["state_store"]
    update = store.update
    writing = asyncio.Event()

    async def slow_update(*args, **kwargs):
        writing.set()
        await asyncio.sleep(0.3)
        return await update(*args, **kwargs)

    runtime = _worker(backends, drain_timeout=0.1, drain_cancel_timeout=0.1)
    await runtime.start()
    handle = await runtime.submit(Work(n=1))
    store.update = slow_update  # the run's RUNNING write is under way at the drain's end
    await writing.wait()

    await _stop(runtime)

    assert handler.log == []
    state = await handle.status()
    assert (state.status, state.attempt) == (JobStatus.SUBMITTED, 0)
    assert await backends["queue"].claim(["default"], visibility_timeout=30) is not None


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_handler_ignoring_its_cancellation_does_not_hold_up_the_stop(backends):
    handler = Handler(on_cancel="ignore")
    _register(handler)
    runtime = _worker(backends, drain_timeout=0.1, drain_cancel_timeout=0.2)
    await runtime.start()
    handle = await runtime.submit(Work(n=1))
    await handler.started.wait()

    try:
        assert await _stop(runtime) < 1
        assert handler.log == [("start", 1), ("cancelled", 1)]
        # Its claim is left to expire: the job is not handed on under it.
        assert (await handle.status()).status == JobStatus.RUNNING
        assert await backends["queue"].claim(["default"], visibility_timeout=30) is None
    finally:
        handler.release.set()
        await asyncio.sleep(0.05)


async def test_a_handler_turning_its_cancellation_into_an_error_is_handed_back_not_failed():
    backends = {"queue": InMemoryQueue(), "state_store": InMemoryStateStore()}
    handler = Handler(on_cancel="convert")
    _register(handler, max_attempts=1)
    runtime = _worker(backends, drain_timeout=0.1)
    await runtime.start()
    handle = await runtime.submit(Work(n=1))
    await handler.started.wait()

    await _stop(runtime)

    state = await handle.status()
    assert (state.status, state.attempt, state.last_error) == (JobStatus.SUBMITTED, 0, None)
    assert await runtime.inspect_dlq() == []
    assert await backends["queue"].claim(["default"], visibility_timeout=30) is not None


async def test_a_job_resumed_from_a_pause_keeps_its_paused_state_when_handed_back():
    backends = {"queue": InMemoryQueue(), "state_store": InMemoryStateStore()}
    seen = []
    release = asyncio.Event()

    @skrift.handler("drain.resumable")
    async def resumable(job: Work, context):
        seen.append(dict(context.paused_state))
        if not context.paused_state:
            return Pause(state={"step": 2})
        await release.wait()
        return "done"

    runtime = _worker(backends, drain_timeout=0.1)
    await runtime.start()
    handle = await runtime.submit("drain.resumable", {"n": 1})
    while len(seen) < 1 or (await handle.status()).status != JobStatus.PAUSED:
        await asyncio.sleep(0.01)
    await runtime.wake(handle.id)
    while len(seen) < 2:
        await asyncio.sleep(0.01)

    await _stop(runtime)

    state = await handle.status()
    assert (state.status, state.paused_state) == (JobStatus.SUBMITTED, {"step": 2})
    release.set()
    successor = _worker(backends)
    await successor.start()
    try:
        assert await asyncio.wait_for(successor.handle(handle.id).result(), 2) == "done"
    finally:
        await asyncio.wait_for(successor.stop(), 5)
    assert seen == [{}, {"step": 2}, {"step": 2}]


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_stop_waits_for_an_ack_already_under_way(backends):
    handler = Handler(seconds=0)
    _register(handler)
    queue = backends["queue"]
    ack = queue.ack
    acking = asyncio.Event()

    async def slow_ack(*args, **kwargs):
        acking.set()
        await asyncio.sleep(0.4)
        return await ack(*args, **kwargs)

    queue.ack = slow_ack
    runtime = _worker(backends, drain_timeout=0.05, drain_cancel_timeout=0.05)
    await runtime.start()
    handle = await runtime.submit(Work(n=1))
    await acking.wait()

    await _stop(runtime)

    state = await handle.status()
    assert (state.status, state.result) == (JobStatus.COMPLETED, 1)
    del queue.ack
    assert await queue.claim(["default"], visibility_timeout=30) is None


def test_the_default_drain_ends_inside_kubernetes_grace_period():
    from skrift.config import WorkersConfig

    config = WorkersConfig()
    assert config.drain_timeout + config.drain_cancel_timeout < 30


def test_the_worker_process_uses_the_configured_drain():
    from unittest.mock import MagicMock

    from skrift.cli import _configure_worker_runtime
    from skrift.config import WorkersConfig

    settings = MagicMock()
    settings.workers = WorkersConfig(drain_timeout=7, drain_cancel_timeout=2)
    runtime = _configure_worker_runtime(
        settings, session_maker=None, queues=["default"], concurrency=1
    )
    assert (runtime.config.drain_timeout, runtime.config.drain_cancel_timeout) == (7, 2)
