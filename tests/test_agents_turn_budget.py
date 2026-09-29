"""A turn's retry budget survives resubmission with a fresh job id (#140).

Each run job has its own ``max_attempts``, but activating a pending turn gives
it a fresh job id and so a fresh attempt count. In #140 one user message was
activated 24,336 times, each time failing the same deterministic way, because
something kept putting the turn back on the pending queue. These tests put a
failed turn's payload back the same way (the refill itself is still
unexplained) and check the turn is never run again.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import agents_run_dead, register_agent_handlers
from skrift.agents.state import load_runstate, stream_name, update_runstate
from skrift.hooks import AGENT_EVENT_APPENDED, hooks
from skrift.workers.models import DeadJobEntry, DeadLetterCause, JobEnvelope
from skrift.workers.registry import registry as worker_registry

REFILLS = 10


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


def _broken_agent(where: str):
    """An agent whose every run fails the same way, and a count of its runs.

    ``deps_factory`` fails before the agent loop and ``tool`` fails inside it;
    either way the job's attempts run out and it dead-letters as
    RETRIES_EXHAUSTED (#202). The ``tool`` model calls the tool on every
    request: a failed turn's messages, which the next turn sees (#230), end in
    a tool return, and TestModel answers with text after one.
    """

    runs = {"count": 0}

    def deps_factory(ctx):
        if where == "deps_factory":
            runs["count"] += 1
            return ctx.deps_ref["account_id"]
        return None

    def call_boom(messages, info):
        return ModelResponse(parts=[ToolCallPart("boom", {})])

    agent = skrift.Agent(
        FunctionModel(call_boom) if where == "tool" else TestModel(call_tools=[]),
        name="broken",
        deps_factory=deps_factory,
    )

    @agent.tool
    async def boom(ctx: RunContext) -> str:
        runs["count"] += 1
        raise RuntimeError("deterministic")

    return agent, runs


def _refill_on_activation(session_id: str, payload: dict):
    """Put ``payload`` back on the pending queue each time it is activated, up
    to REFILLS times, as whatever refilled the queue in #140 did."""

    refills = {"count": 0}

    async def on_event(event_type, event_payload, runstate):
        if runstate.session_id != session_id or event_type != "UserMessageActivated":
            return
        if event_payload["turn_id"] != payload["turn_id"] or refills["count"] >= REFILLS:
            return
        refills["count"] += 1

        async def refill(state):
            state.pending_user_messages.append(dict(payload))
            return state

        await update_runstate(session_id, refill)

    hooks.add_action(AGENT_EVENT_APPENDED, on_event)
    return refills


async def _settled(session_id: str):
    for _ in range(500):
        state = await load_runstate(session_id)
        if (
            state.status == "failed"
            and state.current_run_job_id is None
            and not state.pending_user_messages
            and not state.outbox
        ):
            return state
        await asyncio.sleep(0.02)
    raise AssertionError(f"session never settled: {state.status}, {len(state.pending_user_messages)} pending")


@pytest.mark.parametrize("where", ["deps_factory", "tool"])
async def test_a_failed_turn_put_back_on_the_pending_queue_is_not_run_again(
    where, clean_hooks, caplog
):
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",), poll_interval=0.01)
    agent, runs = _broken_agent(where)
    session = await agent.run("hi", dispatch="queued", deps_ref={})
    turn_id = await session.send("again")
    payload = (await load_runstate(session.id)).pending_user_messages[0]
    refills = _refill_on_activation(session.id, payload)

    await runtime.start()
    try:
        with caplog.at_level(logging.WARNING, logger="skrift.agents.runtime"):
            state = await asyncio.wait_for(_settled(session.id), 15)
    finally:
        await asyncio.wait_for(runtime.stop(), 5)

    assert refills["count"] == 1
    assert runs["count"] == 2 * 3  # the first turn, then "again" once, 3 attempts each
    assert turn_id in state.turn_errors
    events = [event for _, event in await runtime.event_log.read(stream_name(session.id))]
    event_types = [event["type"] for event in events]
    assert event_types.count("UserMessageReceived") == 2  # "hi" and "again"
    assert event_types.count("UserMessageActivated") == 1
    assert event_types.count("AgentFailed") == 2  # once per turn, not per attempt
    assert f"turn {turn_id}" in caplog.text


async def test_a_turn_that_fails_inside_the_loop_is_retried(clean_hooks):
    # A failure inside the agent loop gets the job's attempts, as one before it
    # does (#202): here the tool fails once, and the retry completes the turn.
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",), poll_interval=0.01)
    agent = skrift.Agent(TestModel(call_tools=["flaky"]), name="flaky")
    calls = {"count": 0}

    @agent.tool
    async def flaky(ctx: RunContext) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient")
        return "ok"

    session = await agent.run("hi", dispatch="queued")
    await runtime.start()
    try:
        for _ in range(500):
            state = await load_runstate(session.id)
            if state.terminal_at is not None:
                break
            await asyncio.sleep(0.02)
    finally:
        await asyncio.wait_for(runtime.stop(), 5)

    assert state.status == "completed"
    assert calls["count"] == 2
    assert state.turn_errors == {}
    events = [event for _, event in await runtime.event_log.read(stream_name(session.id))]
    event_types = [event["type"] for event in events]
    assert "AgentFailed" not in event_types
    assert event_types.count("AgentCompleted") == 1


def _dead(session_id: str, job_id: str) -> DeadJobEntry:
    return DeadJobEntry(
        job=JobEnvelope(
            id=job_id, type="agents.run", queue="agents", payload={"session_id": session_id}
        ),
        queue="agents",
        job_type="agents.run",
        cause=DeadLetterCause.RETRIES_EXHAUSTED,
    )


async def test_a_dead_letter_skips_a_copy_of_the_failed_turn_and_runs_the_next_turn():
    skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", dispatch="queued")
    first = await session.send("first")
    second = await session.send("second")
    state = await load_runstate(session.id)
    first_payload = state.pending_user_messages[0]

    await agents_run_dead(_dead(session.id, state.current_run_job_id))  # "hi" fails
    state = await load_runstate(session.id)
    assert state.current_turn_id == first

    async def put_back(state):
        state.pending_user_messages.insert(0, dict(first_payload))
        return state

    await update_runstate(session.id, put_back)
    await agents_run_dead(_dead(session.id, state.current_run_job_id))  # "first" fails

    state = await load_runstate(session.id)
    assert state.status == "queued"
    assert state.current_turn_id == second
    assert state.pending_user_messages == []
    user_turns = [m.get("turn_id") for m in state.messages if m.get("role") == "user"]
    assert user_turns[1:] == [first, second]
