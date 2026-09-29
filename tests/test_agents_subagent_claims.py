"""A parent agent awaiting a sub-agent on the in-process worker pool (#141).

The parent's run holds its worker's claim while it waits. With the in-memory
queue the claim is kept alive for as long as it waits, so it is not reclaimed
and run a second time. If every worker is already waiting on a sub-agent, none
is left to run the sub-agent, and the wait fails at once instead of hanging.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
from skrift.agents.session import AgentSessionError
from skrift.agents.worker_slot import NoFreeWorkerError
from skrift.workers.registry import registry as worker_registry

# Long enough that a claim renewed every third of it survives a starved event
# loop (#229).
LEASE = 1.0


@pytest.fixture(autouse=True)
def clean_registries():
    worker_registry.clear()
    register_agent_handlers()
    agent_registry.clear()
    skrift.set_blob_store(InMemoryBlobStore())
    yield
    worker_registry.clear()
    agent_registry.clear()
    skrift.configure_workers(mode="inline")


def _parent_and_child(dispatch, *, child_seconds):
    calls = {"parent_tool": 0, "child_tool": 0}
    child = skrift.Agent(
        TestModel(call_tools=["slow"], custom_output_text="child done"), name="child"
    )
    parent = skrift.Agent(
        TestModel(call_tools=["ask_child"], custom_output_text="parent done"), name="parent"
    )

    @child.tool
    async def slow(ctx: RunContext) -> str:
        calls["child_tool"] += 1
        await asyncio.sleep(child_seconds)
        return "slow"

    @parent.tool
    async def ask_child(ctx: RunContext) -> str:
        calls["parent_tool"] += 1
        session = await child.run("go", dispatch=dispatch)
        return str(await session.result())

    return parent, calls


async def _run_parent(concurrency, dispatch, *, child_seconds=1.5 * LEASE):
    parent, calls = _parent_and_child(dispatch, child_seconds=child_seconds)
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents", "agents-priority"),
        concurrency=concurrency,
        visibility_timeout=LEASE,
        reaper_interval=0.05,
        poll_interval=0.01,
    )
    await runtime.start()
    try:
        session = await parent.run("go", dispatch="queued")
        try:
            return await asyncio.wait_for(session.result(), 10), calls
        finally:
            # Let the pool finish any sub-agent still queued.
            await asyncio.sleep(0.1)
    finally:
        await runtime.stop()


@pytest.mark.parametrize("dispatch", ["queued", "inline"])
async def test_a_parent_awaiting_a_sub_agent_keeps_its_claim(dispatch, caplog):
    concurrency = 2 if dispatch == "queued" else 1
    with caplog.at_level(logging.WARNING, logger="skrift.workers.runtime"):
        result, calls = await _run_parent(concurrency, dispatch)

    assert result == "parent done"
    assert calls == {"parent_tool": 1, "child_tool": 1}
    assert "Worker loop error" not in caplog.text
    assert "lost its claim" not in caplog.text


async def test_awaiting_a_queued_sub_agent_on_the_only_worker_fails_fast():
    with pytest.raises(AgentSessionError, match="no in-process worker is free"):
        await _run_parent(1, "queued", child_seconds=0)


async def test_awaiting_a_sub_agent_that_already_finished_needs_no_free_worker():
    # An inline sub-agent has finished before its result is awaited.
    result, calls = await _run_parent(1, "inline", child_seconds=0)

    assert result == "parent done"
    assert calls == {"parent_tool": 1, "child_tool": 1}


async def test_in_memory_claims_are_renewed_only_by_their_holder():
    from skrift.workers import InMemoryQueue
    from skrift.workers.models import JobEnvelope, utcnow

    queue = InMemoryQueue()
    await queue.submit(JobEnvelope(type="long"))
    claimed = await queue.claim(["default"], visibility_timeout=0.05)
    job_id = claimed.job.id

    assert queue.claim_held_by("default", job_id, asyncio.current_task()) == claimed.token
    other = asyncio.create_task(asyncio.sleep(0))
    assert queue.claim_held_by("default", job_id, other) is None
    await other

    assert await queue.renew_claim("default", job_id, "not-the-token", visibility_timeout=60) is False
    assert await queue.renew_claim("default", job_id, claimed.token, visibility_timeout=60)
    # A shorter renewal never cuts a lease short.
    assert await queue.renew_claim("default", job_id, claimed.token, visibility_timeout=0.01)
    await asyncio.sleep(0.1)
    await queue._release_expired_claims(utcnow())
    assert await queue.claim(["default"], visibility_timeout=60) is None

    await queue.ack("default", job_id, claimed.token)
    assert await queue.renew_claim("default", job_id, claimed.token, visibility_timeout=60) is False


def _parent_with_background_waiter(*, wait_before_returning):
    """A parent whose tool leaves a task awaiting a queued sub-agent's result.

    The waiter starts waiting once ``go`` is set. With ``wait_before_returning``
    the tool keeps the parent's worker until the waiter has finished.
    """

    go, child_gate = asyncio.Event(), asyncio.Event()
    waiters: list[asyncio.Task] = []
    child = skrift.Agent(
        TestModel(call_tools=["gated"], custom_output_text="child done"), name="child"
    )
    parent = skrift.Agent(
        TestModel(call_tools=["spawn_waiter"], custom_output_text="parent done"), name="parent"
    )

    @child.tool
    async def gated(ctx: RunContext) -> str:
        await child_gate.wait()
        return "gated"

    @parent.tool
    async def spawn_waiter(ctx: RunContext) -> str:
        session = await child.run("go", dispatch="queued")

        async def wait_for_child():
            await go.wait()
            return await session.result()

        waiters.append(asyncio.create_task(wait_for_child()))
        if wait_before_returning:
            go.set()
            await asyncio.wait(waiters)
        return "spawned"

    return parent, go, child_gate, waiters


async def _one_worker():
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents", "agents-priority"),
        concurrency=1,
        visibility_timeout=LEASE,
        reaper_interval=0.05,
        poll_interval=0.01,
    )
    await runtime.start()
    return runtime


async def test_a_waiter_left_behind_by_a_finished_run_waits_for_its_sub_agent():
    parent, go, child_gate, waiters = _parent_with_background_waiter(wait_before_returning=False)
    runtime = await _one_worker()
    try:
        session = await parent.run("go", dispatch="queued")
        assert await asyncio.wait_for(session.result(), 10) == "parent done"
        # The parent's run has freed the only worker, which now runs the child.
        go.set()
        await asyncio.sleep(0.1)
        child_gate.set()
        assert await asyncio.wait_for(waiters[0], 10) == "child done"
    finally:
        child_gate.set()
        await runtime.stop()


async def test_a_waiter_spawned_while_its_run_holds_the_only_worker_fails_fast():
    parent, _go, child_gate, waiters = _parent_with_background_waiter(wait_before_returning=True)
    runtime = await _one_worker()
    try:
        session = await parent.run("go", dispatch="queued")
        assert await asyncio.wait_for(session.result(), 10) == "parent done"
        with pytest.raises(NoFreeWorkerError, match="no in-process worker is free"):
            await waiters[0]
    finally:
        child_gate.set()
        await asyncio.sleep(0.1)
        await runtime.stop()


async def test_a_waiter_left_behind_does_not_count_as_a_waiting_worker():
    child_gate = asyncio.Event()
    waiters: list[asyncio.Task] = []
    child = skrift.Agent(
        TestModel(call_tools=["gated"], custom_output_text="child done"), name="child"
    )
    leaver = skrift.Agent(
        TestModel(call_tools=["spawn_waiter"], custom_output_text="leaver done"), name="leaver"
    )
    asker = skrift.Agent(
        TestModel(call_tools=["ask_child"], custom_output_text="asker done"), name="asker"
    )

    @child.tool
    async def gated(ctx: RunContext) -> str:
        await child_gate.wait()
        return "gated"

    @leaver.tool
    async def spawn_waiter(ctx: RunContext) -> str:
        session = await child.run("go", dispatch="queued")
        waiters.append(asyncio.create_task(session.result()))
        await asyncio.sleep(0.05)  # the waiter is waiting while this run holds a worker
        return "spawned"

    @asker.tool
    async def ask_child(ctx: RunContext) -> str:
        session = await child.run("go", dispatch="queued")
        return str(await session.result())

    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents", "agents-priority"),
        concurrency=2,
        visibility_timeout=LEASE,
        reaper_interval=0.05,
        poll_interval=0.01,
    )
    await runtime.start()
    try:
        left = await leaver.run("go", dispatch="queued")
        assert await asyncio.wait_for(left.result(), 10) == "leaver done"
        # The leaver's worker is free again; the asker's wait is the only one
        # a worker is in.
        asked = await asker.run("go", dispatch="queued")
        await asyncio.sleep(0.2)
        child_gate.set()
        assert await asyncio.wait_for(asked.result(), 10) == "asker done"
        assert await asyncio.wait_for(waiters[0], 10) == "child done"
    finally:
        child_gate.set()
        await asyncio.sleep(0.1)
        await runtime.stop()
