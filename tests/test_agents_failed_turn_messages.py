"""A turn that raises mid-run keeps the messages it produced (#230).

The run's messages used to reach the session only when the run paused or
completed, so a turn that failed inside the agent loop lost every tool call,
tool result and model response it produced, and the next turn started from the
failed turn's bare prompt. A usage limit was also retried, spending the whole
budget again on each attempt from the same messages.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import UsageLimits

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.models import AgentRunJob
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import agents_run_dead, agents_run_handler, register_agent_handlers
from skrift.agents.state import load_runstate, stream_name
from skrift.workers import PermanentFailure, WorkerContext
from skrift.workers.models import DeadJobEntry, DeadLetterCause, JobEnvelope, Pause
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


async def _settled(session_id: str, status: str):
    for _ in range(500):
        state = await load_runstate(session_id)
        if state.status == status and state.current_run_job_id is None and not state.outbox:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError(f"session never reached {status}: {state.status}")


def _history(state) -> list:
    return ModelMessagesTypeAdapter.validate_python(
        [message["content"] for message in state.messages if message["role"] == "model"]
    )


def _prompts(messages) -> list:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def _parts(messages, kind) -> list:
    return [part for message in messages for part in message.parts if isinstance(part, kind)]


def _answers_again(messages) -> bool:
    return "again" in _prompts(messages[-1:])


async def test_a_turn_that_raises_mid_run_keeps_its_messages_for_the_next_turn():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",), poll_interval=0.01)
    seen = []

    def respond(messages, info):
        seen.append(list(messages))
        if _answers_again(messages):
            return ModelResponse(parts=[TextPart("done")])
        returns = _parts(messages[-1:], ToolReturnPart)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("search", {"q": "one"}, tool_call_id="c1")])
        return ModelResponse(parts=[ToolCallPart("boom", {}, tool_call_id="c2")])

    agent = skrift.Agent(FunctionModel(respond), name="mid_turn")

    @agent.tool
    async def search(ctx: RunContext, q: str) -> str:
        return f"found {q}"

    @agent.tool
    async def boom(ctx: RunContext) -> str:
        raise RuntimeError("the service is down")

    session = await agent.run("hi", dispatch="queued")
    await runtime.start()
    try:
        state = await _settled(session.id, "failed")
        history = _history(state)
        # One copy of the last attempt's run, not one per attempt, ending in a
        # failed return for the tool call the run never answered.
        assert _prompts(history) == ["hi"]
        assert [call.tool_name for call in _parts(history, ToolCallPart)] == ["search", "boom"]
        returns = _parts(history, ToolReturnPart)
        assert [(part.tool_name, part.outcome) for part in returns] == [
            ("search", "success"),
            ("boom", "failed"),
        ]
        assert returns[0].content == "found one"
        assert "RuntimeError" in returns[1].content
        assert state.failed_run_messages is None

        await session.send("again")
        state = await _settled(session.id, "completed")
    finally:
        await asyncio.wait_for(runtime.stop(), 5)

    assert state.output == "done"
    sent = seen[-1]
    assert _prompts(sent) == ["hi", "again"]
    assert [call.tool_name for call in _parts(sent, ToolCallPart)] == ["search", "boom"]
    assert [part.tool_call_id for part in _parts(sent, ToolReturnPart)] == ["c1", "c2"]


async def test_a_usage_limit_fails_the_turn_once_and_keeps_its_messages():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",), poll_interval=0.01)
    seen = []

    def respond(messages, info):
        seen.append(list(messages))
        if _answers_again(messages):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart("search", {"q": "more"}, tool_call_id=f"c{len(seen)}")]
        )

    agent = skrift.Agent(FunctionModel(respond), name="limited")

    @agent.tool
    async def search(ctx: RunContext, q: str) -> str:
        return f"found {q}"

    session = await agent.run(
        "hi", dispatch="queued", usage_limits=UsageLimits(request_limit=3)
    )
    turn_id = (await load_runstate(session.id)).current_turn_id
    await runtime.start()
    try:
        state = await _settled(session.id, "failed")
        # Three requests, then the limit: one attempt, not max_attempts of them.
        assert len(seen) == 3
        error = state.turn_errors[turn_id]
        assert error["exception_type"] == "UsageLimitExceeded"
        assert "request_limit of 3" in error["exception_message"]
        history = _history(state)
        assert _prompts(history) == ["hi"]
        assert len(_parts(history, ToolCallPart)) == 3
        assert [part.outcome for part in _parts(history, ToolReturnPart)] == ["success"] * 3

        await session.send("again", usage_limits=UsageLimits(request_limit=3))
        state = await _settled(session.id, "completed")
    finally:
        await asyncio.wait_for(runtime.stop(), 5)

    assert state.output == "done"
    assert _prompts(seen[-1]) == ["hi", "again"]
    assert [part.content for part in _parts(seen[-1], ToolReturnPart)] == ["found more"] * 3
    events = [event for _, event in await runtime.event_log.read(stream_name(session.id))]
    failed = [event for event in events if event["type"] == "AgentFailed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["cause"] == "permanent_failure"


def _part_kinds(state) -> list:
    return [
        [part["part_kind"] for part in message["content"]["parts"]]
        for message in state.messages
        if message["role"] == "model"
    ]


async def test_a_retried_turn_that_completes_keeps_only_its_completed_run():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",), poll_interval=0.01)
    calls = {"count": 0}

    steady = skrift.Agent(TestModel(call_tools=["lookup"]), name="steady")
    flaky = skrift.Agent(TestModel(call_tools=["lookup"]), name="flaky")

    @steady.tool
    async def lookup(ctx: RunContext) -> str:
        return "ok"

    @flaky.tool(name="lookup")
    async def flaky_lookup(ctx: RunContext) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient")
        return "ok"

    steady_session = await steady.run("hi", dispatch="queued")
    flaky_session = await flaky.run("hi", dispatch="queued")
    await runtime.start()
    try:
        steady_state = await _settled(steady_session.id, "completed")
        flaky_state = await _settled(flaky_session.id, "completed")
    finally:
        await asyncio.wait_for(runtime.stop(), 5)

    assert calls["count"] == 2
    assert flaky_state.failed_run_messages is None
    assert _part_kinds(flaky_state) == _part_kinds(steady_state)
    assert _prompts(_history(flaky_state)) == ["hi"]


async def test_a_last_attempt_that_fails_before_the_loop_keeps_nothing_from_an_earlier_one():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",), poll_interval=0.01)
    attempts = {"count": 0}

    def deps_factory(ctx):
        attempts["count"] += 1
        if attempts["count"] > 1:
            raise RuntimeError("before the loop")
        return None

    def call_boom(messages, info):
        return ModelResponse(parts=[ToolCallPart("boom", {})])

    agent = skrift.Agent(FunctionModel(call_boom), name="mixed", deps_factory=deps_factory)

    @agent.tool
    async def boom(ctx: RunContext) -> str:
        raise RuntimeError("inside the loop")

    session = await agent.run("hi", dispatch="queued", deps_ref={})
    turn_id = (await load_runstate(session.id)).current_turn_id
    await runtime.start()
    try:
        state = await _settled(session.id, "failed")
    finally:
        await asyncio.wait_for(runtime.stop(), 5)

    assert attempts["count"] == 3
    assert state.turn_errors[turn_id]["exception_message"] == "before the loop"
    assert _history(state) == []
    assert state.failed_run_messages is None


def _gated_boom_agent():
    """An agent whose tool raises on its first call and waits for ``gate`` on
    later ones, setting ``entered`` first."""

    def call_boom(messages, info):
        return ModelResponse(parts=[ToolCallPart("boom", {})])

    agent = skrift.Agent(FunctionModel(call_boom), name="gated")
    calls = {"count": 0}
    entered = asyncio.Event()
    gate = asyncio.Event()

    @agent.tool
    async def boom(ctx: RunContext) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("inside the loop")
        entered.set()
        await gate.wait()
        return "ok"

    return agent, entered, gate


async def _run(runtime, job_id: str, claim_order: int):
    envelope = (await runtime.get_job_state(job_id)).job
    context = WorkerContext(
        runtime=runtime, job=envelope, paused_state={}, claim_order=claim_order
    )
    return await agents_run_handler(AgentRunJob.model_validate(envelope.payload), context)


async def _failed_first_attempt(runtime, agent):
    session = await agent.run("hi", dispatch="queued")
    job_id = (await load_runstate(session.id)).current_run_job_id
    with pytest.raises(RuntimeError, match="inside the loop"):
        await _run(runtime, job_id, 1)
    kept = (await load_runstate(session.id)).failed_run_messages
    assert kept is not None and kept.messages
    return session, job_id


async def _dead_letter(session_id: str, job_id: str):
    await agents_run_dead(
        DeadJobEntry(
            job=JobEnvelope(
                id=job_id, type="agents.run", queue="agents", payload={"session_id": session_id}
            ),
            queue="agents",
            job_type="agents.run",
            cause=DeadLetterCause.RETRIES_EXHAUSTED,
        )
    )
    return await load_runstate(session_id)


async def test_a_cancelled_attempt_drops_what_an_earlier_attempt_kept():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent, entered, gate = _gated_boom_agent()
    session, job_id = await _failed_first_attempt(runtime, agent)

    second = asyncio.create_task(_run(runtime, job_id, 2))
    await asyncio.wait_for(entered.wait(), 5)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert (await load_runstate(session.id)).failed_run_messages is None

    state = await _dead_letter(session.id, job_id)
    assert state.status == "failed"
    assert _history(state) == []


async def test_a_paused_attempt_drops_what_an_earlier_attempt_kept():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent, entered, gate = _gated_boom_agent()
    session, job_id = await _failed_first_attempt(runtime, agent)

    second = asyncio.create_task(_run(runtime, job_id, 2))
    await asyncio.wait_for(entered.wait(), 5)
    await session.pause()
    gate.set()
    assert isinstance(await asyncio.wait_for(second, 5), Pause)
    assert (await load_runstate(session.id)).failed_run_messages is None

    state = await _dead_letter(session.id, job_id)
    assert state.status == "failed"
    assert _history(state) == []


async def test_a_stale_claim_that_is_cancelled_keeps_what_its_successor_kept():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))

    def call_boom(messages, info):
        return ModelResponse(parts=[ToolCallPart("boom", {})])

    agent = skrift.Agent(FunctionModel(call_boom), name="stale_cancel")
    calls = {"count": 0}
    entered = asyncio.Event()

    @agent.tool
    async def boom(ctx: RunContext) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            entered.set()
            await asyncio.Event().wait()
        raise RuntimeError("inside the loop")

    session = await agent.run("hi", dispatch="queued")
    job_id = (await load_runstate(session.id)).current_run_job_id
    stale = asyncio.create_task(_run(runtime, job_id, 1))
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(RuntimeError, match="inside the loop"):
        await _run(runtime, job_id, 2)
    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale

    kept = (await load_runstate(session.id)).failed_run_messages
    assert kept is not None and kept.messages


@pytest.mark.parametrize("ending", ["permanent", "cancelled"])
async def test_a_failed_read_while_keeping_messages_leaves_the_runs_exception(ending, monkeypatch):
    # Keeping a run's messages must not replace the exception the worker
    # classifies the run by, even when the store fails.
    import skrift.agents.runtime as agent_runtime

    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    started = asyncio.Event()

    async def deps_factory(ctx):
        started.set()
        if ending == "permanent":
            raise PermanentFailure("no account")
        await asyncio.Event().wait()

    agent = skrift.Agent(TestModel(), name="read_fails", deps_factory=deps_factory)
    session = await agent.run("hi", dispatch="queued", deps_ref={})
    job_id = (await load_runstate(session.id)).current_run_job_id
    load = agent_runtime.load_runstate

    async def failing_load(session_id):
        if started.is_set():
            raise OSError("store unavailable")
        return await load(session_id)

    monkeypatch.setattr(agent_runtime, "load_runstate", failing_load)
    run = asyncio.create_task(_run(runtime, job_id, 1))
    await asyncio.wait_for(started.wait(), 5)
    if ending == "permanent":
        with pytest.raises(PermanentFailure, match="no account"):
            await run
    else:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run


async def test_cancelling_a_session_drops_what_a_failed_attempt_kept():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent, entered, gate = _gated_boom_agent()
    session, job_id = await _failed_first_attempt(runtime, agent)

    await session.cancel()
    state = await load_runstate(session.id)
    assert state.status == "cancelled"
    assert state.failed_run_messages is None
