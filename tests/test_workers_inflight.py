"""A worker keeping several jobs in flight at once (#143).

``workers.max_inflight_per_worker`` lets each in-process worker run up to that
many claimed jobs at once, so jobs that mostly wait on I/O do not each need a
worker of their own. A worker still polls the queue as one: a single claim at
a time, and only while it has a free place. The default of 1 keeps each worker
running one job at a time.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
from skrift.agents.session import AgentSessionError
from skrift.db.base import Base
from skrift.workers import (
    InMemoryDeadLetterStore,
    InMemoryEventLog,
    InMemoryQueue,
    InMemoryStateStore,
    RedisEventLog,
    RedisQueue,
    RedisStateStore,
    SQLAlchemyDeadLetterStore,
    SQLAlchemyEventLog,
    SQLAlchemyQueue,
    SQLAlchemyStateStore,
    WorkerConfig,
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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inflight.db'}")
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
        "state_store": RedisStateStore(client=fake_redis_client, prefix="test:inflight"),
        "event_log": RedisEventLog(client=fake_redis_client, prefix="test:inflight"),
        "queue": RedisQueue(client=fake_redis_client, prefix="test:inflight"),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


def _worker(backends, **config):
    config.setdefault("poll_interval", 0.01)
    config.setdefault("max_poll_interval", 0.02)
    return skrift.configure_workers(mode="in_process", **backends, **config)


class Blocking:
    """A handler whose runs wait for ``release``, counting how many run at once."""

    def __init__(self):
        self.running = 0
        self.most = 0
        self.cancelled = 0
        self.release = asyncio.Event()

    async def __call__(self, job: Work):
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1
        return job.n

    async def until_running(self, count):
        for _ in range(300):
            if self.running >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{self.running} running, expected {count}")


def _register(handler, name="inflight.work"):
    @skrift.handler(name)
    async def work(job: Work):
        return await handler(job)


class CountingClaims:
    """Wraps a queue's claim, counting calls and how many are in flight at once."""

    def __init__(self, queue):
        self.claim = queue.claim
        self.calls = 0
        self.inflight = 0
        self.most = 0
        queue.claim = self

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        self.inflight += 1
        self.most = max(self.most, self.inflight)
        try:
            await asyncio.sleep(0.005)
            return await self.claim(*args, **kwargs)
        finally:
            self.inflight -= 1


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
@pytest.mark.parametrize(("concurrency", "inflight"), [(1, 1), (1, 3), (2, 2)])
async def test_each_worker_runs_up_to_its_inflight_cap_at_once(backends, concurrency, inflight):
    handler = Blocking()
    _register(handler)
    config = {} if inflight == 1 else {"max_inflight_per_worker": inflight}
    runtime = _worker(backends, concurrency=concurrency, **config)
    capacity = concurrency * inflight
    handles = [await runtime.submit(Work(n=n)) for n in range(capacity + 2)]
    await runtime.start()
    try:
        await handler.until_running(capacity)
        await asyncio.sleep(0.2)
        assert (handler.running, handler.most) == (capacity, capacity)

        handler.release.set()
        results = [await asyncio.wait_for(handle.result(), 5) for handle in handles]
    finally:
        handler.release.set()
        await runtime.stop()
    assert results == list(range(capacity + 2))
    assert handler.most == capacity


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_worker_polls_with_one_claim_at_a_time(backends):
    handler = Blocking()
    _register(handler)
    claims = CountingClaims(backends["queue"])
    runtime = _worker(backends, max_inflight_per_worker=4)
    await runtime.start()
    try:
        await asyncio.sleep(0.2)  # idle, every place free
        handles = [await runtime.submit(Work(n=n)) for n in range(2)]
        await handler.until_running(2)
        await asyncio.sleep(0.2)  # two places busy, two free
        assert claims.most == 1
        handler.release.set()
        for handle in handles:
            await asyncio.wait_for(handle.result(), 5)
    finally:
        handler.release.set()
        await runtime.stop()


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_an_idle_worker_polls_no_more_often_than_one_running_a_job_at_a_time(backends):
    # Its places share one poll backoff, so free places add no queries.
    claims = CountingClaims(backends["queue"])
    runtime = _worker(
        backends, max_inflight_per_worker=4, poll_interval=0.02, max_poll_interval=0.02
    )
    await runtime.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await runtime.stop()
    # A single poller manages at most 0.5 / (0.02 + 0.005) = 20 claims.
    assert claims.calls <= 22


async def test_a_worker_whose_queue_fails_retries_it_no_more_often_than_one_place():
    queue = InMemoryQueue()
    claims = 0

    async def failing_claim(*args, **kwargs):
        nonlocal claims
        claims += 1
        raise ConnectionError("queue unreachable")

    queue.claim = failing_claim
    runtime = _worker({"queue": queue}, max_inflight_per_worker=4, poll_interval=0.05)
    await runtime.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await runtime.stop()
    # One retry per poll_interval: at most 0.3 / 0.05 + 1 = 7 claims.
    assert 1 <= claims <= 8


@pytest.mark.parametrize("finishes", [True, False], ids=["finishing", "handed-back"])
async def test_stopping_a_worker_drains_every_job_it_has_in_flight(finishes):
    handler = Blocking()
    _register(handler)
    runtime = _worker(
        {"queue": InMemoryQueue(), "state_store": InMemoryStateStore()},
        max_inflight_per_worker=3,
        drain_timeout=2 if finishes else 0.1,
    )
    handles = [await runtime.submit(Work(n=n)) for n in range(3)]
    await runtime.start()
    await handler.until_running(3)

    if finishes:
        asyncio.get_running_loop().call_later(0.2, handler.release.set)
    assert await asyncio.wait_for(runtime.stop(), 5) == []

    assert (handler.running, handler.cancelled) == (0, 0 if finishes else 3)
    for handle in handles:
        state = await handle.status()
        if finishes:
            assert (state.status, state.result) == (JobStatus.COMPLETED, state.job.payload["n"])
        else:
            assert (state.status, state.attempt) == (JobStatus.SUBMITTED, 0)


def test_max_inflight_per_worker_defaults_to_one_and_reaches_the_worker_process():
    from unittest.mock import MagicMock

    from pydantic import ValidationError

    from skrift.cli import _configure_worker_runtime
    from skrift.config import WorkersConfig

    assert WorkersConfig().max_inflight_per_worker == 1
    with pytest.raises(ValidationError):
        WorkersConfig(max_inflight_per_worker=0)

    settings = MagicMock()
    settings.workers = WorkersConfig(max_inflight_per_worker=8)
    runtime = _configure_worker_runtime(
        settings, session_maker=None, queues=["default"], concurrency=1
    )
    assert runtime.config.max_inflight_per_worker == 8


@pytest.mark.parametrize("setting", ["concurrency", "max_inflight_per_worker"])
def test_configuring_workers_in_python_rejects_a_pool_with_no_places(setting):
    # A pool with no places would start and never claim a job.
    with pytest.raises(ValueError, match=setting):
        skrift.configure_workers(mode="in_process", **{setting: 0})
    with pytest.raises(ValueError, match=setting):
        WorkerConfig(**{setting: -1})


# Sub-agents awaited on the in-process pool (#141) count a worker's places.


@pytest.fixture
def agents():
    register_agent_handlers()
    agent_registry.clear()
    skrift.set_blob_store(InMemoryBlobStore())
    yield
    agent_registry.clear()


LEASE = 0.2


def _nested_agents(depth, *, seconds):
    """Agent ``level0`` awaits a queued ``level1``, which awaits ``level2``, and
    so on; the last one's tool sleeps ``seconds``."""

    calls: dict[int, int] = {}

    def agent(level):
        last = level == depth - 1
        model = TestModel(
            call_tools=["slow" if last else "ask"], custom_output_text=f"level{level} done"
        )
        built = skrift.Agent(model, name=f"level{level}")
        if last:

            @built.tool
            async def slow(ctx: RunContext) -> str:
                calls[level] = calls.get(level, 0) + 1
                await asyncio.sleep(seconds)
                return "slow"

        else:
            child = agent(level + 1)

            @built.tool
            async def ask(ctx: RunContext) -> str:
                calls[level] = calls.get(level, 0) + 1
                session = await child.run("go", dispatch="queued")
                return str(await session.result())

        return built

    return agent(0), calls


async def _run_nested(depth, *, inflight, seconds=4 * LEASE):
    top, calls = _nested_agents(depth, seconds=seconds)
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents", "agents-priority"),
        concurrency=1,
        max_inflight_per_worker=inflight,
        visibility_timeout=LEASE,
        reaper_interval=0.02,
        poll_interval=0.01,
    )
    await runtime.start()
    try:
        session = await top.run("go", dispatch="queued")
        return await asyncio.wait_for(session.result(), 10), calls
    finally:
        await asyncio.sleep(0.1)
        await runtime.stop()


async def test_a_sub_agent_runs_on_its_parents_worker_while_the_parent_waits(agents, caplog):
    # One worker with two places: the parent waits in one, the sub-agent runs in
    # the other, and both keep their claims past the lease.
    result, calls = await _run_nested(2, inflight=2)

    assert result == "level0 done"
    assert calls == {0: 1, 1: 1}
    assert "lost its claim" not in caplog.text
    assert "Worker loop error" not in caplog.text


async def test_awaiting_a_sub_agent_with_every_place_waiting_fails_fast(agents):
    with pytest.raises(AgentSessionError, match="no in-process worker is free"):
        await _run_nested(3, inflight=2, seconds=0)
