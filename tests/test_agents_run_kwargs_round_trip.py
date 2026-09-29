"""Run kwargs holding Pydantic AI dataclasses survive a JSON state store (#235).

The SQLAlchemy and Redis state stores save RunState as JSON, which turns a
``UsageLimits`` (or ``RunUsage``, a message in ``message_history``, a builtin
tool) into a plain dict. The run passed that dict to Pydantic AI, which failed
before its first model request, so a queued run on those stores could not use
``usage_limits`` at all.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

import pytest
from pydantic_ai import RunContext
from pydantic_ai.builtin_tools import WebSearchTool
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage, UsageLimits

import skrift
from skrift.agents.blob import InMemoryBlobStore
from skrift.agents.models import RunState
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import register_agent_handlers
from skrift.agents.state import load_runstate
from skrift.agents.session import Session
from skrift.agents.turns import decode_turn_kwargs
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


async def _worker_runtime(backend, stack, tmp_path):
    """An in-process worker runtime on the given queue/state backend."""
    if backend == "memory":
        return skrift.configure_workers(
            mode="in_process", queues=("agents-priority", "agents"), poll_interval=0.01
        )

    from skrift.workers import (
        RedisEventLog,
        RedisQueue,
        RedisStateStore,
        SQLAlchemyEventLog,
        SQLAlchemyQueue,
        SQLAlchemyStateStore,
    )

    if backend == "sqlalchemy":
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from skrift.db import models as _models  # noqa: F401 - register the worker tables
        from skrift.db.base import Base

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agents.db'}")
        stack.push_async_callback(engine.dispose)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        return skrift.configure_workers(
            mode="in_process",
            queues=("agents-priority", "agents"),
            poll_interval=0.01,
            state_store=SQLAlchemyStateStore(session_maker=session_maker),
            event_log=SQLAlchemyEventLog(session_maker=session_maker),
            queue=SQLAlchemyQueue(session_maker=session_maker),
        )
    import fakeredis.aioredis as fake_aioredis

    client = fake_aioredis.FakeRedis()
    stack.push_async_callback(client.aclose)
    await client.flushall()
    return skrift.configure_workers(
        mode="in_process",
        queues=("agents-priority", "agents"),
        poll_interval=0.01,
        state_store=RedisStateStore(client=client, prefix="test:agents"),
        event_log=RedisEventLog(client=client, prefix="test:agents"),
        queue=RedisQueue(client=client, prefix="test:agents"),
    )


def _searching_agent(name: str):
    """An agent whose model answers "first" and calls its search tool on every
    other request."""
    seen = []

    def respond(messages, info):
        seen.append(list(messages))
        if _prompts(messages)[-1] == "first":
            return ModelResponse(parts=[TextPart("ok")])
        return ModelResponse(
            parts=[ToolCallPart("search", {"q": "more"}, tool_call_id=f"c{len(seen)}")]
        )

    agent = skrift.Agent(FunctionModel(respond), name=name)

    @agent.tool
    async def search(ctx: RunContext, q: str) -> str:
        return f"found {q}"

    return agent, seen


async def _turn_errors(session_id: str, count: int) -> RunState:
    for _ in range(500):
        state = await load_runstate(session_id)
        if len(state.turn_errors) == count and state.current_run_job_id is None:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn errors never reached {count}: {state.turn_errors}")


def _prompts(messages) -> list:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


@pytest.mark.parametrize("backend", ["memory", "sqlalchemy", "redis"])
async def test_a_queued_turns_usage_limit_is_enforced_on_every_state_store(backend, tmp_path):
    agent, seen = _searching_agent("limited")
    async with AsyncExitStack() as stack:
        runtime = await _worker_runtime(backend, stack, tmp_path)
        session = await agent.run("first", dispatch="queued")
        # Sent while the first turn is queued, so its kwargs wait in
        # pending_user_messages and reach RunState.run_kwargs on activation.
        second = await session.send("second", usage_limits=UsageLimits(request_limit=1))
        assert (await load_runstate(session.id)).pending_user_messages[0]["turn_id"] == second

        await runtime.start()
        stack.push_async_callback(lambda: asyncio.wait_for(runtime.stop(), 5))
        await _turn_errors(session.id, 1)
        # Sent to the failed session, so send() writes RunState.run_kwargs.
        third = await session.send("third", usage_limits=UsageLimits(request_limit=2))
        state = await _turn_errors(session.id, 2)

    assert state.status == "failed"
    for turn_id, limit in ((second, 1), (third, 2)):
        error = state.turn_errors[turn_id]
        assert error["exception_type"] == "UsageLimitExceeded"
        assert f"request_limit of {limit}" in error["exception_message"]
    # One request for the second turn, two for the third, and no retries.
    assert [_prompts(messages)[-1] for messages in seen] == ["first", "second", "third", "third"]


@pytest.mark.parametrize("backend", ["sqlalchemy", "redis"])
async def test_message_history_and_usage_reach_a_queued_run_on_a_json_state_store(
    backend, tmp_path
):
    agent, seen = _searching_agent("carried")
    history = [
        ModelRequest(parts=[UserPromptPart("earlier")]),
        ModelResponse(parts=[TextPart("noted")]),
    ]
    async with AsyncExitStack() as stack:
        runtime = await _worker_runtime(backend, stack, tmp_path)
        session = await agent.run(
            "now",
            dispatch="queued",
            message_history=history,
            # One request already spent of two: the run gets one more.
            usage=RunUsage(requests=1),
            usage_limits=UsageLimits(request_limit=2),
        )
        turn_id = (await load_runstate(session.id)).current_turn_id
        await runtime.start()
        stack.push_async_callback(lambda: asyncio.wait_for(runtime.stop(), 5))
        state = await _turn_errors(session.id, 1)

    assert state.turn_errors[turn_id]["exception_type"] == "UsageLimitExceeded"
    assert len(seen) == 1
    assert _prompts(seen[0]) == ["earlier", "now"]
    assert seen[0][1].parts == [TextPart("noted")]


def test_decoding_rebuilds_dataclass_kwargs_from_json_and_keeps_objects():
    history = [
        ModelRequest(parts=[UserPromptPart("earlier")]),
        ModelResponse(parts=[TextPart("noted")]),
    ]

    def dynamic_tool(ctx):
        return WebSearchTool()

    kwargs = {
        "usage_limits": UsageLimits(request_limit=2, total_tokens_limit=100),
        "usage": RunUsage(requests=1, input_tokens=5, details={"reasoning": 3}),
        "message_history": history,
        "builtin_tools": [WebSearchTool(max_uses=2)],
        "model_settings": {"temperature": 0.2},
    }
    stored = RunState(session_id="s", agent_name="a", run_kwargs=kwargs).model_dump(mode="json")
    assert isinstance(stored["run_kwargs"]["usage_limits"], dict)

    decoded = decode_turn_kwargs(RunState.model_validate(stored).run_kwargs)

    assert decoded == kwargs
    # The in-memory store keeps the objects, including ones JSON cannot carry.
    live = decode_turn_kwargs({**kwargs, "builtin_tools": [WebSearchTool(), dynamic_tool]})
    assert live["usage_limits"] is kwargs["usage_limits"]
    assert live["message_history"][0] is history[0]
    assert live["builtin_tools"][1] is dynamic_tool


@pytest.mark.parametrize("dispatch", ["queued", "inline"])
@pytest.mark.parametrize("backend", ["sqlalchemy", "redis"])
async def test_a_model_instance_is_refused_when_the_state_store_saves_json(
    backend, dispatch, tmp_path
):
    # Inline dispatch runs its turn from the stored kwargs too, so it would
    # fail the same way in the worker.
    agent = skrift.Agent(TestModel(custom_output_text="default"), name="named")
    async with AsyncExitStack() as stack:
        await _worker_runtime(backend, stack, tmp_path)
        with pytest.raises(TypeError, match="Pass model by name"):
            await agent.run("hi", dispatch=dispatch, session_id="s1", model=TestModel())
        assert await load_runstate("s1") is None

        session = await agent.run("hi", dispatch="queued", session_id="s2", model="test")
        stored = await load_runstate(session.id)
        assert stored.run_kwargs["model"] == "test"
        with pytest.raises(TypeError, match="Pass model by name"):
            await session.send("again", model=TestModel())
        state = await load_runstate(session.id)
        assert state.messages == stored.messages
        assert state.pending_user_messages == []


@pytest.mark.parametrize("dispatch", ["queued", "inline"])
async def test_the_in_memory_store_runs_a_model_instance(dispatch):
    runtime = skrift.configure_workers(
        mode="in_process", queues=("agents-priority", "agents"), poll_interval=0.01
    )
    agent = skrift.Agent(TestModel(custom_output_text="default"), name="named")
    session = await agent.run(
        "hi", dispatch=dispatch, model=TestModel(custom_output_text="override")
    )
    await runtime.start()
    try:
        assert await asyncio.wait_for(session.result(), 5) == "override"
    finally:
        await asyncio.wait_for(runtime.stop(), 5)


@dataclass(kw_only=True)
class _TaggedLimits(UsageLimits):
    """A UsageLimits whose extra field JSON keeps but UsageLimits drops."""

    tag: str


@pytest.mark.parametrize(
    "kwargs",
    [
        {"usage_limits": _TaggedLimits(tag="mine", request_limit=2)},
        {"message_history": ["not a message"]},
    ],
    ids=["usage_limits subclass", "message_history item"],
)
@pytest.mark.parametrize("backend", ["sqlalchemy", "redis"])
async def test_a_kwarg_that_would_not_come_back_the_same_is_refused(backend, kwargs, tmp_path):
    agent = skrift.Agent(TestModel(), name="rebuilt")
    async with AsyncExitStack() as stack:
        await _worker_runtime(backend, stack, tmp_path)
        with pytest.raises(TypeError, match="cannot store"):
            await agent.run("hi", dispatch="queued", session_id="s1", **kwargs)
        assert await load_runstate("s1") is None
        # A dict is stored as it is, and one the worker rebuilds is accepted.
        await agent.run("hi", dispatch="queued", usage_limits={"request_limit": 2})
