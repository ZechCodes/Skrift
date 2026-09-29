"""A governor that gates a worker's claims at runtime (#207).

``workers.governor`` names a callable the in-process pool asks, within a
worker's poll turn and just before its claim, whether the worker takes another
job: ``governor(runtime, current_inflight) -> bool``, sync or async. A no, a
raise, or anything but a bool claims nothing and waits as an empty poll does.
``max_inflight_per_worker`` still caps the worker, since a worker whose places
are all busy doesn't poll. A stopping pool asks nothing, and admits nothing a
governor answers once the stop has begun.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
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
)
from skrift.workers.models import JobStatus
from skrift.workers.registry import registry

BACKENDS = ["memory", "sqlalchemy", "redis"]
LOGGER = "skrift.workers.runtime"


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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'governor.db'}")
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
        "state_store": RedisStateStore(client=fake_redis_client, prefix="test:governor"),
        "event_log": RedisEventLog(client=fake_redis_client, prefix="test:governor"),
        "queue": RedisQueue(client=fake_redis_client, prefix="test:governor"),
        "dead_letter_store": InMemoryDeadLetterStore(),
    }


def _memory():
    return {"queue": InMemoryQueue(), "state_store": InMemoryStateStore()}


def _worker(backends, governor, **config):
    config.setdefault("poll_interval", 0.01)
    config.setdefault("max_poll_interval", 0.02)
    return skrift.configure_workers(
        mode="in_process", governor=governor, **backends, **config
    )


class Governor:
    """Answers each call with the first of ``answers``, dropping it unless it
    is the last, which repeats; an exception answer is raised. Records the
    ``current_inflight`` of each call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen: list[int] = []

    async def __call__(self, runtime, current_inflight):
        self.seen.append(current_inflight)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def asked(self, times):
        await _until(lambda: len(self.seen) >= times)


class Blocking:
    """A handler whose runs wait for ``release``."""

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


def _register(handler, name="governor.work"):
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
            return await self.claim(*args, **kwargs)
        finally:
            self.inflight -= 1


async def _until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def _timed_stop(runtime):
    started = time.monotonic()
    abandoned = await asyncio.wait_for(runtime.stop(), 10)
    return abandoned, time.monotonic() - started


# Gating


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_governor_decides_whether_a_worker_claims(backends):
    governor = Governor(False)
    handler = Blocking()
    handler.release.set()
    _register(handler)
    runtime = _worker(backends, governor)
    handle = await runtime.submit(Work(n=1))
    await runtime.start()
    try:
        await governor.asked(3)
        assert (await handle.status()).status == JobStatus.SUBMITTED

        governor.answers = [True]
        assert await asyncio.wait_for(handle.result(), 5) == 1
    finally:
        await runtime.stop()


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_the_inflight_cap_binds_whatever_the_governor_answers(backends):
    governor = Governor(True)
    handler = Blocking()
    _register(handler)
    runtime = _worker(backends, governor, max_inflight_per_worker=2)
    handles = [await runtime.submit(Work(n=n)) for n in range(4)]
    await runtime.start()
    try:
        await _until(lambda: handler.running == 2)
        asked = len(governor.seen)
        await asyncio.sleep(0.2)
        # With both places busy the worker doesn't poll, so it isn't asked.
        assert (handler.most, len(governor.seen)) == (2, asked)
        handler.release.set()
        assert [await asyncio.wait_for(h.result(), 5) for h in handles] == [0, 1, 2, 3]
    finally:
        handler.release.set()
        await runtime.stop()
    assert handler.most == 2
    assert max(governor.seen) == 1


async def test_current_inflight_counts_the_workers_places_holding_a_job():
    governor = Governor(True)
    handler = Blocking()
    _register(handler)
    runtime = _worker(_memory(), governor, max_inflight_per_worker=4)
    for n in range(3):
        await runtime.submit(Work(n=n))
    await runtime.start()
    try:
        await _until(lambda: handler.running == 3)
        await governor.asked(5)
        # Each claim is followed by an ask counting it; then a free place idles.
        assert governor.seen[:4] == [0, 1, 2, 3]
        assert set(governor.seen[3:]) == {3}

        handler.release.set()
        await _until(lambda: governor.seen[-1] == 0)
    finally:
        handler.release.set()
        await runtime.stop()


@pytest.mark.parametrize("backends", BACKENDS, indirect=True)
async def test_a_worker_asks_once_per_poll_not_once_per_place(backends):
    governor = Governor(True)
    claims = CountingClaims(backends["queue"])
    runtime = _worker(
        backends, governor, max_inflight_per_worker=4, poll_interval=0.02, max_poll_interval=0.02
    )
    await runtime.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await runtime.stop()
    # One ask before each claim, from one poller: at most 0.5 / 0.02 + 1 polls.
    assert len(governor.seen) - claims.calls in (0, 1)
    assert 5 <= len(governor.seen) <= 27
    assert claims.most == 1


async def test_a_no_backs_off_as_an_empty_poll_does():
    governor = Governor(False)
    queue = InMemoryQueue()
    claims = CountingClaims(queue)
    runtime = _worker(
        {"queue": queue},
        governor,
        max_inflight_per_worker=4,
        poll_interval=0.01,
        max_poll_interval=0.08,
    )
    await runtime.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await runtime.stop()
    # Waits of 0.01, 0.02, 0.04, then 0.08: about 9 asks in 0.5 s, not 50.
    assert 4 <= len(governor.seen) <= 11
    assert claims.calls == 0


async def test_a_sync_governor_is_called_directly():
    seen = []

    def allow(runtime, current_inflight):
        seen.append(current_inflight)
        return True

    handler = Blocking()
    handler.release.set()
    _register(handler)
    runtime = _worker(_memory(), allow)
    handle = await runtime.submit(Work(n=7))
    await runtime.start()
    try:
        assert await asyncio.wait_for(handle.result(), 5) == 7
    finally:
        await runtime.stop()
    assert seen and seen[0] == 0


async def test_a_governor_with_false_truthiness_still_gates():
    class Closed:
        calls = 0

        def __bool__(self):
            return False

        def __call__(self, runtime, current_inflight):
            Closed.calls += 1
            return False

    _register(Blocking())
    runtime = _worker(_memory(), Closed())
    handle = await runtime.submit(Work(n=1))
    await runtime.start()
    try:
        await _until(lambda: Closed.calls >= 3)
        assert (await handle.status()).status == JobStatus.SUBMITTED
    finally:
        await runtime.stop()


# Failing governors fail closed


@pytest.mark.parametrize("answer", [RuntimeError("metrics down"), None, 1, "yes", object()])
async def test_a_failing_governor_claims_nothing_and_warns_once(answer, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    governor = Governor(answer)
    _register(Blocking())
    runtime = _worker(_memory(), governor)
    handle = await runtime.submit(Work(n=1))
    await runtime.start()
    try:
        await governor.asked(4)
        assert (await handle.status()).status == JobStatus.SUBMITTED
    finally:
        await runtime.stop()
    warnings = [r for r in caplog.records if r.getMessage().startswith("Worker governor")]
    assert [r.levelno for r in warnings] == [logging.WARNING]


async def test_a_healthy_answer_ends_a_failure_streak(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    failure = RuntimeError("metrics down")
    governor = Governor(failure, failure, False, False, failure)
    _register(Blocking())
    runtime = _worker(_memory(), governor)
    handle = await runtime.submit(Work(n=1))
    await runtime.start()
    try:
        await governor.asked(8)
        assert (await handle.status()).status == JobStatus.SUBMITTED
    finally:
        await runtime.stop()
    lines = [
        (r.levelno, r.getMessage().split(";")[0])
        for r in caplog.records
        if r.getMessage().startswith("Worker governor")
    ]
    assert lines == [
        (logging.WARNING, "Worker governor raised"),
        (logging.INFO, "Worker governor answered False again"),
        (logging.WARNING, "Worker governor raised"),
    ]


async def test_inflight_is_released_after_a_run_that_errors(caplog):
    governor = Governor(True)
    handler = Blocking()
    handler.release.set()
    _register(handler)
    runtime = _worker(_memory(), governor, max_inflight_per_worker=2)
    execute_claim = runtime.execute_claim

    async def failing(claimed, **kwargs):
        runtime.execute_claim = execute_claim
        raise RuntimeError("execute failed")

    runtime.execute_claim = failing
    await runtime.submit(Work(n=1))
    await runtime.start()
    try:
        await _until(lambda: "Worker loop error" in caplog.text)
        asked = len(governor.seen)
        await governor.asked(asked + 2)
        assert governor.seen[-1] == 0
    finally:
        await runtime.stop()


async def test_inflight_is_released_after_a_settlement_that_errors(caplog):
    governor = Governor(True)
    handler = Blocking()
    handler.release.set()
    _register(handler)
    queue = InMemoryQueue()
    ack = queue.ack

    async def failing_ack(*args, **kwargs):
        queue.ack = ack
        raise ConnectionError("queue unreachable")

    queue.ack = failing_ack
    runtime = _worker({"queue": queue}, governor, max_inflight_per_worker=2)
    await runtime.submit(Work(n=1))
    await runtime.start()
    try:
        await _until(lambda: "Worker loop error" in caplog.text)
        asked = len(governor.seen)
        await governor.asked(asked + 2)
        assert governor.seen[-1] == 0
    finally:
        await runtime.stop()


# Stopping


async def test_a_stop_during_a_no_ends_promptly_and_asks_nothing_more():
    governor = Governor(False)
    runtime = _worker(_memory(), governor, poll_interval=5, max_poll_interval=5)
    await runtime.start()
    await governor.asked(1)

    abandoned, took = await _timed_stop(runtime)
    asked = len(governor.seen)
    await asyncio.sleep(0.1)
    assert (abandoned, len(governor.seen)) == ([], asked)
    assert took < 1


async def test_a_stop_while_the_governor_answers_admits_nothing():
    # A governor that catches its cancellation and says yes admits nothing.
    entered = asyncio.Event()

    async def stubborn(runtime, current_inflight):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return True

    queue = InMemoryQueue()
    claims = CountingClaims(queue)
    _register(Blocking())
    runtime = _worker({"queue": queue}, stubborn, max_inflight_per_worker=3)
    handle = await runtime.submit(Work(n=1))
    await runtime.start()
    await asyncio.wait_for(entered.wait(), 5)

    abandoned, took = await _timed_stop(runtime)
    assert (abandoned, claims.calls) == ([], 0)
    assert (await handle.status()).status == JobStatus.SUBMITTED
    assert took < 1


async def test_a_governor_that_lets_cancellation_through_does_not_hold_a_stop():
    entered = asyncio.Event()

    async def hanging(runtime, current_inflight):
        entered.set()
        await asyncio.Event().wait()

    runtime = _worker(_memory(), hanging, max_inflight_per_worker=3, drain_timeout=5)
    await runtime.start()
    await asyncio.wait_for(entered.wait(), 5)

    abandoned, took = await _timed_stop(runtime)
    assert abandoned == []
    assert took < 1  # well under drain_timeout


async def test_a_pool_with_busy_asking_and_waiting_places_stops_and_restarts():
    # One place runs a job, one waits on the governor holding the worker's
    # turn, and one waits for the turn; the pool stops, and starts again.
    asking = asyncio.Event()

    async def one_at_a_time(runtime, current_inflight):
        if current_inflight == 0:
            return True
        asking.set()
        await asyncio.Event().wait()

    handler = Blocking()
    _register(handler)
    runtime = _worker(
        _memory(), one_at_a_time, max_inflight_per_worker=3, drain_timeout=0.1
    )
    handle = await runtime.submit(Work(n=5))
    for round_ in range(3):
        asking.clear()
        await runtime.start()
        await _until(lambda: handler.running == 1)
        await asyncio.wait_for(asking.wait(), 5)

        abandoned, took = await _timed_stop(runtime)
        assert (abandoned, handler.running, handler.cancelled) == ([], 0, round_ + 1)
        assert (await handle.status()).status == JobStatus.SUBMITTED
        assert took < 2

    handler.release.set()
    await runtime.start()
    try:
        assert await asyncio.wait_for(handle.result(), 5) == 5
    finally:
        await runtime.stop()


# Awaited sub-agents


@pytest.fixture
def agents():
    register_agent_handlers()
    agent_registry.clear()
    skrift.set_blob_store(InMemoryBlobStore())
    yield
    agent_registry.clear()


async def test_a_parent_whose_sub_agent_the_governor_refuses_still_drains(agents):
    # The documented deadlock: a governor that allows one job in flight never
    # admits the queued child its parent waits on. The drain still hands the
    # parent back, releasing its claim and stopping the task that renews it.
    child = skrift.Agent(TestModel(custom_output_text="child done"), name="child")
    parent = skrift.Agent(TestModel(call_tools=["ask"]), name="parent")

    @parent.tool
    async def ask(ctx: RunContext) -> str:
        session = await child.run("go", dispatch="queued")
        return str(await session.result())

    seen = []

    async def one_in_flight(runtime, current_inflight):
        seen.append(current_inflight)
        return current_inflight < 1

    queue = InMemoryQueue()
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents", "agents-priority"),
        max_inflight_per_worker=2,
        poll_interval=0.01,
        max_poll_interval=0.02,
        drain_timeout=0.2,
        queue=queue,
        governor=one_in_flight,
    )
    await runtime.start()
    await parent.run("go", dispatch="queued")
    await _until(lambda: seen.count(1) >= 3)  # the child is refused
    stats = await queue.stats("agents")
    assert (stats.ready, stats.claimed) == (1, 1)

    abandoned, took = await _timed_stop(runtime)
    stats = await queue.stats("agents")
    assert (abandoned, stats.ready, stats.claimed) == ([], 2, 0)
    assert took < 2
    keepers = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__name__", "") == "_keep_claim"
    ]
    assert keepers == []


# Configuration


def allow_all(runtime, current_inflight):
    return True


NOT_CALLABLE = 3


def test_the_governor_defaults_to_none_and_reaches_the_worker_process():
    from skrift.cli import _configure_worker_runtime
    from skrift.config import WorkersConfig

    assert WorkersConfig().governor is None
    settings = MagicMock()
    settings.workers = WorkersConfig(governor=f"{__name__}:allow_all")
    runtime = _configure_worker_runtime(
        settings, session_maker=None, queues=["default"], concurrency=1
    )
    assert runtime.config.governor is allow_all

    settings.workers = WorkersConfig()
    runtime = _configure_worker_runtime(
        settings, session_maker=None, queues=["default"], concurrency=1
    )
    assert runtime.config.governor is None


def test_the_web_app_passes_the_governor_when_it_configures_workers():
    # The web app configures workers in its startup hook; its call must pass
    # the governor as the worker process's does.
    source = Path(inspect.getfile(skrift)).parent / "asgi.py"
    calls = [
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "configure_workers"
    ]
    assert len(calls) == 1
    governor = [kw.value for kw in calls[0].keywords if kw.arg == "governor"]
    assert [ast.unparse(value) for value in governor] == ["settings.workers.governor"]


@pytest.mark.parametrize("spec", ["no_colon", ":allow_all", f"{__name__}:"])
def test_a_malformed_governor_path_is_rejected_by_the_config(spec):
    from skrift.config import WorkersConfig

    with pytest.raises(ValidationError, match="module:attribute"):
        WorkersConfig(governor=spec)


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        ("skrift_no_such_module:governor", ModuleNotFoundError),
        (f"{__name__}:no_such_governor", AttributeError),
        (f"{__name__}:NOT_CALLABLE", TypeError),
    ],
)
def test_a_bad_governor_path_fails_when_workers_are_configured(spec, error):
    with pytest.raises(error):
        skrift.configure_workers(mode="in_process", governor=spec)
