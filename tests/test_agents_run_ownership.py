"""Only the run that owns a session's current turn writes its outcome (#204).

A run whose claim expired keeps running until it notices. Meanwhile a later
claim of the same job runs the turn, and may finish it and start the next.
These tests run both claims' handlers directly, with their claim orders, and
let the stale one fail or complete last.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.models import AgentRunJob
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import agents_run_handler, register_agent_handlers
from skrift.agents.state import load_runstate
from skrift.workers import WorkerContext
from skrift.workers.registry import handler
from skrift.workers.registry import registry as worker_registry


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


def _agent_with_gated_tool_calls(stale_outcome: str = "fails"):
    """An agent whose first tool call, the stale run's, waits for its gate and
    then raises (or returns, if ``stale_outcome`` is "completes"); its second,
    the later claim's, waits for its gate and returns."""

    agent = skrift.Agent(TestModel(call_tools=["lookup"]), name="owned")
    calls = {"count": 0}
    entered = [asyncio.Event(), asyncio.Event()]
    gates = [asyncio.Event(), asyncio.Event()]

    @agent.tool
    async def lookup(ctx: RunContext) -> str:
        calls["count"] += 1
        call = calls["count"] - 1
        if call < 2:
            entered[call].set()
            await gates[call].wait()
            if call == 0 and stale_outcome == "fails":
                raise RuntimeError("the stale run's tool failed")
        return "ok"

    return agent, entered, gates


async def _run(runtime, job_id: str, claim_order: int | None):
    """Run ``job_id``'s handler as the claim with ``claim_order``."""
    envelope = (await runtime.get_job_state(job_id)).job
    context = WorkerContext(
        runtime=runtime, job=envelope, paused_state={}, claim_order=claim_order
    )
    return await agents_run_handler(AgentRunJob.model_validate(envelope.payload), context)


@pytest.mark.parametrize("stale_outcome", ["fails", "completes"])
@pytest.mark.parametrize("orders", [(1, 2), (None, None)], ids=["ordered", "unordered"])
async def test_a_stale_run_does_not_settle_the_turn_its_successor_started(orders, stale_outcome):
    # "unordered": a queue that reports no claim order; the job id alone tells
    # the stale run that the session has moved on.
    stale_order, fresh_order = orders
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent, entered, gates = _agent_with_gated_tool_calls(stale_outcome)
    gates[1].set()
    session = await agent.run("hi", dispatch="queued")
    second = await session.send("again")
    first_job = (await load_runstate(session.id)).current_run_job_id

    stale = asyncio.create_task(_run(runtime, first_job, stale_order))
    try:
        await asyncio.wait_for(entered[0].wait(), 5)
        # The job's later claim runs the first turn and starts the second.
        await asyncio.wait_for(_run(runtime, first_job, fresh_order), 5)
        state = await load_runstate(session.id)
        assert state.current_turn_id == second
        second_job = state.current_run_job_id
    finally:
        gates[0].set()
    try:
        await asyncio.wait_for(stale, 5)
    except RuntimeError:
        pass  # a stale run may still raise, as long as it writes nothing

    state = await load_runstate(session.id)
    assert state.turn_errors == {}
    assert second not in state.turn_results
    assert state.status == "queued"
    assert state.current_run_job_id == second_job

    await asyncio.wait_for(_run(runtime, second_job, 1), 5)
    state = await load_runstate(session.id)
    assert state.status == "completed"
    assert second in state.turn_results
    assert state.error is None


@pytest.mark.parametrize("stale_outcome", ["fails", "completes"])
async def test_a_stale_run_does_not_settle_the_turn_a_later_claim_is_running(stale_outcome):
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent, entered, gates = _agent_with_gated_tool_calls(stale_outcome)
    session = await agent.run("hi", dispatch="queued")
    job = (await load_runstate(session.id)).current_run_job_id

    stale = asyncio.create_task(_run(runtime, job, 1))
    fresh = None
    try:
        await asyncio.wait_for(entered[0].wait(), 5)
        # A later claim of the same job starts the same turn, and the stale run
        # finishes while that one is still in its tool call.
        fresh = asyncio.create_task(_run(runtime, job, 2))
        await asyncio.wait_for(entered[1].wait(), 5)
        gates[0].set()
        try:
            await asyncio.wait_for(stale, 5)
        except RuntimeError:
            pass  # a stale run may still raise, as long as it writes nothing
        state = await load_runstate(session.id)
        assert state.status == "running"
        assert state.current_run_job_id == job
        assert state.failed_run_messages is None
    finally:
        gates[0].set()
        gates[1].set()
    await asyncio.wait_for(fresh, 5)

    state = await load_runstate(session.id)
    assert state.status == "completed"
    assert state.error is None
    assert state.turn_errors == {}


class _Probe(BaseModel):
    n: int = 0


@pytest.mark.parametrize("mode", ["in_process", "inline"])
async def test_a_handler_is_given_its_runs_claim_order(mode):
    orders = []

    @handler("ownership.probe", queue="agents")
    async def probe(payload: _Probe, context: WorkerContext) -> None:
        orders.append(context.claim_order)

    runtime = skrift.configure_workers(mode=mode, queues=("agents",), poll_interval=0.01)
    await runtime.start()
    try:
        first = await runtime.submit("ownership.probe", _Probe())
        second = await runtime.submit("ownership.probe", _Probe())
        await asyncio.wait_for(first.result(), 5)
        await asyncio.wait_for(second.result(), 5)
    finally:
        await runtime.stop()

    if mode == "inline":
        assert orders == [None, None]
    else:
        assert len(orders) == 2 and all(isinstance(order, int) for order in orders)
