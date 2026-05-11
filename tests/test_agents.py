"""Tests for Skrift agents."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import UsageLimits

import skrift
from skrift.agents.session import AgentSessionError
from skrift.agents.blob import ArchiveBlobStore, BLOB_STREAM_PREFIX, InMemoryBlobStore, get_blob_store
from skrift.agents.config import configure_agent_runtime
from skrift.agents.registry import registry as agent_registry
from skrift.agents.runtime import _tool_events_from_messages, register_agent_handlers
from skrift.agents.state import (
    drain_pending_outboxes,
    load_runstate,
    runstate_key,
    stream_name,
    update_runstate,
)
from skrift.config import AgentsConfig
from skrift.lib.hooks import AGENT_EVENT_APPENDED, hooks
from skrift.workers.registry import registry as worker_registry


class ChatAction(BaseModel):
    action: str
    message: str


class MemoryArtifact(BaseModel):
    id: str
    title: str


@pytest.fixture(autouse=True)
def clean_registries():
    worker_registry.clear()
    register_agent_handlers()
    agent_registry.clear()
    skrift.configure_workers(mode="inline")
    skrift.set_blob_store(InMemoryBlobStore())
    yield
    worker_registry.clear()
    agent_registry.clear()
    skrift.set_blob_store(InMemoryBlobStore())


async def test_agent_run_requires_configured_worker_runtime():
    import skrift.workers.runtime as worker_runtime

    worker_runtime._runtime = None
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    with pytest.raises(RuntimeError, match="Worker runtime not configured"):
        await agent.run("hi")


async def test_agent_run_persists_events_and_returns_session_result():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", actor="ada")

    assert await session.status() == "completed"
    assert await session.result() == "hello"

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    assert [event["type"] for _, event in events] == [
        "UserMessageReceived",
        "AgentStarted",
        "AssistantMessageCompleted",
        "AgentCompleted",
    ]
    state = await session.state()
    assert state.current_run_job_id is None
    assert state.outbox == []


async def test_agent_event_appended_hook_fires_after_event_is_durable(clean_hooks):
    calls: list[tuple[str, dict, str, int]] = []

    async def on_event(event_type, payload, runstate):
        events = await skrift.get_runtime().event_log.read(stream_name(runstate.session_id))
        calls.append((event_type, payload, runstate.session_id, len(events)))
        assert runstate.outbox == []

    hooks.add_action(AGENT_EVENT_APPENDED, on_event)
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", actor="ada")

    assert await session.result() == "hello"
    event_types = [event_type for event_type, _, _, _ in calls]
    assert event_types == [
        "UserMessageReceived",
        "AgentStarted",
        "AssistantMessageCompleted",
        "AgentCompleted",
    ]
    assert all(session_id == session.id for _, _, session_id, _ in calls)
    assert [event_count for _, _, _, event_count in calls] == [1, 2, 3, 4]


async def test_agent_event_appended_hook_errors_do_not_break_run(clean_hooks, caplog):
    async def broken_handler(*_args):
        raise RuntimeError("broadcast failed")

    hooks.add_action(AGENT_EVENT_APPENDED, broken_handler)
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", actor="ada")

    assert await session.result() == "hello"
    assert "Agent event hook failed" in caplog.text


async def test_session_steer_records_audit_event():
    skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    await session.steer("focus on brevity", actor={"kind": "service", "id": "test"})

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    assert events[-1][1]["type"] == "SteerInjected"
    assert events[-1][1]["payload"]["text"] == "focus on brevity"


async def test_runner_applies_pending_steers_and_records_cursor():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    await session.steer("focus on brevity", actor={"kind": "service", "id": "test"})
    await runtime.start()
    try:
        assert await session.result() == "hello"
    finally:
        await runtime.stop()

    state = await session.state()
    assert state.cursor["node_kind"] == "End"
    assert state.pending_steers == []
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    assert "SteerApplied" in [event["type"] for _, event in events]


async def test_agent_run_inline_dispatch_executes_without_worker_pool():
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents-priority", "agents"),
    )
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", actor="ada", dispatch="inline")

    assert await session.result() == "hello"
    state = await session.state()
    assert state.status == "completed"
    lifecycle = await runtime.event_log.read("workers:lifecycle")
    assert [event["type"] for _, event in lifecycle] == [
        "job_submitted",
        "job_claimed",
        "job_started",
        "job_completed",
    ]


async def test_agent_run_same_worker_dispatch_executes_inline():
    skrift.configure_workers(mode="in_process", queues=("agents-priority", "agents"))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", dispatch="same_worker")

    assert await session.result() == "hello"


async def test_agent_run_queued_dispatch_waits_for_worker_pool():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", actor="ada", dispatch="queued")

    assert await session.status() == "queued"
    queue_stats = await runtime.queue.stats("agents")
    assert queue_stats.ready == 1


async def test_agent_run_inline_dispatch_pauses_and_resumes_on_approval():
    runtime = skrift.configure_workers(
        mode="in_process",
        queues=("agents-priority", "agents"),
    )
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(approval=True, policy_description="approval required")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada", dispatch="inline")

    assert await session.status() == "awaiting_approval"
    state = await session.state()
    assert state.current_run_job_id is not None
    job_state = await runtime.get_job_state(state.current_run_job_id)
    assert job_state is not None
    assert job_state.job.queue == "agents-priority"
    assert job_state.job.metadata["skrift_dispatch"] == "inline"
    tool_call_id = state.pending_approvals[0]["tool_call_id"]

    await session.approve(tool_call_id, actor="ada", note="ok")

    assert await session.result() == '{"add":0}'


async def test_agent_run_inline_then_queued_dispatch_queues_after_approval():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(approval=True, policy_description="approval required")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run(
        "use the tool",
        actor="ada",
        dispatch="inline_then_queued",
    )

    assert await session.status() == "awaiting_approval"
    state = await session.state()
    assert state.current_run_job_id is not None
    job_state = await runtime.get_job_state(state.current_run_job_id)
    assert job_state is not None
    assert job_state.job.queue == "agents"
    assert job_state.job.metadata["skrift_dispatch"] == "inline_then_queued"
    tool_call_id = state.pending_approvals[0]["tool_call_id"]

    await session.approve(tool_call_id, actor="ada", note="ok")

    assert await session.status() == "queued"
    queue_stats = await runtime.queue.stats("agents")
    assert queue_stats.ready == 1
    await runtime.start()
    try:
        assert await session.result() == '{"add":0}'
    finally:
        await runtime.stop()


async def test_cancel_queued_session_finalizes_without_running():
    skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    await session.cancel(actor="ada")

    state = await session.state()
    assert state.status == "cancelled"
    assert state.terminal_at is not None
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    assert [event["type"] for _, event in events][-2:] == [
        "AgentCancellationRequested",
        "AgentCancelled",
    ]


async def test_pause_and_resume_queued_session():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    await session.pause(actor="ada")
    assert await session.status() == "paused"
    await session.resume(actor="ada")
    assert await session.status() == "queued"

    await runtime.start()
    try:
        assert await session.result() == "hello"
    finally:
        await runtime.stop()

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "AgentPaused" in event_types
    assert event_types.count("AgentResumed") >= 1


async def test_audit_export_returns_agent_events():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    audit = await skrift.audit_export(session.id)

    assert audit.session_id == session.id
    assert audit.agent_name == "demo"
    assert audit.terminal_status == "completed"
    assert [event["type"] for event in audit.events][-1] == "AgentCompleted"


async def test_audit_export_dereferences_large_offloaded_fields():
    large_output = "x" * 270_000
    agent = skrift.Agent(TestModel(custom_output_text=large_output), name="demo")
    session = await agent.run("hi", actor="ada")

    raw_events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    completed = next(event for _, event in raw_events if event["type"] == "AgentCompleted")
    assert completed["payload"]["output"]["_offload"] is True

    audit = await skrift.audit_export(session.id)
    exported_completed = next(event for event in audit.events if event["type"] == "AgentCompleted")
    assert exported_completed["payload"]["output"] == large_output


async def test_session_iterator_dereferences_large_offloaded_fields():
    large_output = "x" * 270_000
    agent = skrift.Agent(TestModel(custom_output_text=large_output), name="demo")
    session = await agent.run("hi", actor="ada")

    async for _, event in session:
        if event["type"] == "AgentCompleted":
            assert event["payload"]["output"] == large_output
            break
    else:
        pytest.fail("AgentCompleted event not yielded")


async def test_archive_blob_store_persists_and_dereferences_large_fields():
    runtime = skrift.get_runtime()
    skrift.set_blob_store(ArchiveBlobStore(runtime.archive))
    large_output = "x" * 270_000
    agent = skrift.Agent(TestModel(custom_output_text=large_output), name="demo")
    session = await agent.run("hi", actor="ada")

    raw_events = await runtime.event_log.read(f"agents:run:{session.id}")
    completed = next(event for _, event in raw_events if event["type"] == "AgentCompleted")
    blob_id = completed["payload"]["output"]["blob_id"]
    archived_blob = await runtime.archive.query_events(f"{BLOB_STREAM_PREFIX}{blob_id}")
    assert archived_blob

    audit = await skrift.audit_export(session.id)
    exported_completed = next(event for event in audit.events if event["type"] == "AgentCompleted")
    assert exported_completed["payload"]["output"] == large_output


async def test_agent_runstate_is_snapshotted_to_archive():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    snapshot = await skrift.get_runtime().archive.latest_state_snapshot(runstate_key(session.id))

    assert snapshot is not None
    assert snapshot.session_id == session.id


async def test_configured_blob_backend_is_applied():
    configure_agent_runtime(
        AgentsConfig(blob_backend="skrift.agents.blob:ArchiveBlobStore")
    )

    assert isinstance(get_blob_store(), ArchiveBlobStore)


async def test_configured_agent_queue_is_used(monkeypatch):
    import skrift.agents.state as agent_state

    runtime = skrift.configure_workers(mode="in_process", queues=("custom-agents",))
    monkeypatch.setattr(
        agent_state,
        "get_agents_config",
        lambda: AgentsConfig(default_queue="custom-agents"),
    )
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")
    state = await load_runstate(session.id)
    job_state = await runtime.get_job_state(state.current_run_job_id)

    assert job_state.job.queue == "custom-agents"


async def test_replay_falls_back_to_archived_events_after_hot_log_prune():
    runtime = skrift.get_runtime()
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")
    stream = f"agents:run:{session.id}"
    rows = await runtime.event_log.read(stream)

    await runtime.archive.bulk_insert_events(
        [(stream, position, event) for position, event in rows]
    )
    await runtime.event_log.delete(stream)

    replayed = await skrift.replay(session.id)

    assert [event["type"] for event in replayed] == [
        "UserMessageReceived",
        "AgentStarted",
        "AssistantMessageCompleted",
        "AgentCompleted",
    ]


async def test_session_send_queues_followup_turn():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    await session.send("again", actor="ada")

    assert await session.result() == "hello"
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert event_types.count("UserMessageReceived") == 2
    assert "AgentResumed" in event_types


async def test_session_send_during_queued_run_processes_pending_turn():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    await session.send("again", actor="ada")
    state = await session.state()
    assert state.pending_user_messages[0]["message"] == "again"

    await runtime.start()
    try:
        assert await session.result() == "hello"
    finally:
        await runtime.stop()

    state = await session.state()
    assert state.pending_user_messages == []
    assert [message.get("content") for message in state.messages if message.get("role") == "user"] == [
        "hi",
        "again",
    ]
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    assert "UserMessageActivated" in [event["type"] for _, event in events]


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
async def test_session_send_revives_failed_or_cancelled_session(terminal_status):
    from skrift.agents.state import update_runstate
    from skrift.workers.models import utcnow

    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    async def force_terminal(state):
        state.status = terminal_status
        state.terminal_at = utcnow()
        state.error = {"exception_message": "boom"} if terminal_status == "failed" else None
        return state

    await update_runstate(session.id, force_terminal)
    await session.send("recover", actor="ada")

    assert await session.result() == "hello"
    state = await session.state()
    assert state.status == "completed"
    assert state.error is None
    assert [message.get("content") for message in state.messages if message.get("role") == "user"][-1] == "recover"


async def test_send_cancels_pending_approvals_and_queues_message():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(approval=True, policy_description="approval required")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        while await session.status() != "awaiting_approval":
            await asyncio.sleep(0.01)
        await session.send("skip that", actor="ada")
        state = await session.state()
    finally:
        await runtime.stop()

    assert state.pending_approvals == []
    assert state.pending_user_messages[0]["message"] == "skip that"
    assert state.deferred_tool_results["approvals"]
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    assert "ToolCallRejected" in [event["type"] for _, event in events]


async def test_duplicate_agent_run_session_id_raises():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    await agent.run("hi", session_id="fixed")

    with pytest.raises(AgentSessionError, match="already exists"):
        await agent.run("again", session_id="fixed")


async def test_agent_constructor_output_type_is_preserved():
    agent = skrift.Agent(
        TestModel(custom_output_args=5),
        name="demo",
        output_type=int,
    )

    session = await agent.run("return a number")

    assert await session.result() == 5


async def test_agent_constructor_output_type_rehydrates_persisted_model_output():
    agent = skrift.Agent(
        TestModel(custom_output_args={"action": "answer", "message": "ok"}),
        name="demo",
        output_type=ChatAction,
    )

    session = await agent.run("classify")

    result = await session.result()
    assert result == ChatAction(action="answer", message="ok")
    assert isinstance(result, ChatAction)


async def test_turn_output_type_rehydrates_after_run_kwargs_change():
    agent = skrift.Agent(
        TestModel(custom_output_args={"action": "answer", "message": "ok"}),
        name="demo",
    )

    session = await agent.run("classify", output_type=ChatAction)
    state = await session.state()
    turn_id = state.current_turn_id
    assert turn_id is not None
    assert state.turn_output_types[turn_id]["__skrift_type__"].endswith(":ChatAction")

    async def clear_run_kwargs(runstate):
        runstate.run_kwargs = {}
        return runstate

    await update_runstate(session.id, clear_run_kwargs)

    result = await session.result(turn_id=turn_id)
    assert result == ChatAction(action="answer", message="ok")
    assert isinstance(result, ChatAction)


async def test_agent_run_kwargs_are_forwarded_to_pydantic_ai():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    session = await agent.run("hi", usage_limits=UsageLimits(request_limit=0))

    with pytest.raises(AgentSessionError, match="UsageLimitExceeded"):
        await session.result()
    state = await session.state()
    assert state.run_kwargs["usage_limits"].request_limit == 0
    assert state.error["exception_type"] == "UsageLimitExceeded"


async def test_chat_send_returns_string_and_reuses_session_by_key():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")

    chat = agent.chat("user:1", actor="ada")
    assert await chat.status() == "idle"
    reply = await chat.send("hi")
    second = await agent.chat("user:1", actor="ada").send("again")

    assert reply == "hello"
    assert second == "hello"
    session = await chat.session()
    assert session is not None
    state = await session.state()
    assert [message.get("content") for message in state.messages if message.get("role") == "user"] == [
        "hi",
        "again",
    ]


async def test_chat_send_typed_supports_output_type():
    agent = skrift.Agent(
        TestModel(custom_output_args={"action": "answer", "message": "ok"}),
        name="demo",
    )

    result = await agent.chat("typed").send_typed("classify", output_type=ChatAction)

    assert result == ChatAction(action="answer", message="ok")


async def test_chat_reasoning_string_and_enum_are_turn_settings():
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    chat = agent.chat("reasoning", actor="ada", reasoning=skrift.ReasoningLevel.LOW)

    await chat.send("hi", reasoning="high", model_settings={"temperature": 0.2})

    session = await chat.session()
    assert session is not None
    state = await session.state()
    assert state.run_kwargs["model_settings"]["temperature"] == 0.2
    assert state.run_kwargs["model_settings"]["thinking"] == "high"
    assert state.run_kwargs["metadata"]["skrift_reasoning"] == "high"

    enum_chat = agent.chat("reasoning-enum", actor="ada", reasoning=skrift.ReasoningLevel.LOW)
    await enum_chat.send("hi")
    enum_session = await enum_chat.session()
    assert enum_session is not None
    enum_state = await enum_session.state()
    assert enum_state.run_kwargs["model_settings"]["thinking"] == "low"


async def test_async_deps_factory_is_awaited():
    async def deps_factory(ctx):
        return f"deps:{ctx.session_id}"

    agent = skrift.Agent(TestModel(), name="demo", deps_type=str, deps_factory=deps_factory)

    @agent.tool
    def dep_value(ctx: RunContext[str]) -> str:
        return ctx.deps

    session = await agent.run("use the tool")

    assert (await session.result()).startswith('{"dep_value":"deps:')


async def test_tool_calls_emit_audit_events():
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")

    assert await session.result() == '{"add":0}'
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "ToolCallStarted" in event_types
    assert "ToolCallExecuting" in event_types
    assert "ToolCallCompleted" in event_types
    completed = next(event for _, event in events if event["type"] == "ToolCallCompleted")
    assert completed["payload"]["result"] == 0


def test_tool_call_started_args_are_always_dicts():
    events = _tool_events_from_messages(
        [
            {
                "parts": [
                    {
                        "part_kind": "tool-call",
                        "tool_call_id": "parsed",
                        "tool_name": "send_message",
                        "args": {"message": "hello"},
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_call_id": "json",
                        "tool_name": "get_weather",
                        "args": '{"location":"NYC"}',
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_call_id": "array",
                        "tool_name": "bad_shape",
                        "args": '["not", "an", "object"]',
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_call_id": "invalid",
                        "tool_name": "bad_json",
                        "args": '{"message":',
                    },
                ]
            }
        ]
    )

    started = [payload for event_type, payload in events if event_type == "ToolCallStarted"]
    assert [payload["args"] for payload in started] == [
        {"message": "hello"},
        {"location": "NYC"},
        {"INVALID_JSON": '["not", "an", "object"]'},
        {"INVALID_JSON": '{"message":'},
    ]
    assert all(isinstance(payload["args"], dict) for payload in started)


async def test_tool_call_started_streams_before_tool_returns():
    skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    @agent.tool_plain
    async def add(x: int, y: int) -> int:
        tool_started.set()
        await release_tool.wait()
        return x + y

    session = await agent.run("use the tool", actor="ada")
    runtime = skrift.get_runtime()
    await runtime.start()
    try:
        await asyncio.wait_for(tool_started.wait(), timeout=1)
        events: list[str] = []

        async def wait_for_started() -> None:
            async for _, event in session:
                events.append(event["type"])
                if event["type"] == "ToolCallStarted":
                    return

        await asyncio.wait_for(wait_for_started(), timeout=1)
        assert "ToolCallCompleted" not in events

        release_tool.set()
        assert await session.result() == '{"add":0}'
    finally:
        await runtime.stop()

    audit = await skrift.audit_export(session.id)
    event_types = [event["type"] for event in audit.events]
    assert event_types.count("ToolCallStarted") == 1
    assert event_types.count("ToolCallExecuting") == 1
    assert event_types.count("ToolCallCompleted") == 1
    assert event_types.index("ToolCallStarted") < event_types.index("ToolCallCompleted")


async def test_tool_can_record_artifact_and_session_lists_it():
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool
    async def create_memory(ctx: RunContext[None]) -> str:
        await skrift.record_artifact(
            ctx,
            {"id": "mem_1", "title": "Remember this"},
            kind="memory",
        )
        return "created"

    session = await agent.run("use the tool", actor="ada")

    assert await session.result() == '{"create_memory":"created"}'
    assert await session.artifacts(kind="memory") == [
        {"id": "mem_1", "title": "Remember this"}
    ]
    assert await session.artifacts(kind="memory", model=MemoryArtifact) == [
        MemoryArtifact(id="mem_1", title="Remember this")
    ]
    trail = await skrift.audit_export(session.id)
    artifact = next(event for event in trail.events if event["type"] == "ToolArtifact")
    assert artifact["payload"]["kind"] == "memory"
    assert artifact["payload"]["tool_name"] == "create_memory"
    assert artifact["payload"]["tool_call_id"]


async def test_attach_artifact_alias_records_artifact():
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool
    async def create_note(ctx: RunContext[None]) -> str:
        await skrift.attach_artifact(ctx, {"id": "note_1"}, kind="note")
        return "created"

    session = await agent.run("use the tool")

    assert await session.artifacts(kind="note") == [{"id": "note_1"}]
    assert await session.artifacts(kind="memory") == []


async def test_hitl_approval_pauses_and_resumes_tool_call():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(approval=True, policy_description="approval required")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        async def wait_for_approval() -> None:
            while await session.status() != "awaiting_approval":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_approval(), timeout=1)
        state = await session.state()
        tool_call_id = state.pending_approvals[0]["tool_call_id"]

        await session.approve(tool_call_id, actor="ada", note="ok")

        assert await session.result() == '{"add":0}'
    finally:
        await runtime.stop()

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "ToolCallAwaitingApproval" in event_types
    assert "ToolCallApproved" in event_types
    assert "ToolCallCompleted" in event_types


async def test_callable_approval_false_runs_tool_inline():
    decisions: list[tuple[int, int]] = []
    agent = skrift.Agent(TestModel(), name="demo")

    def needs_approval(x: int, y: int) -> bool:
        decisions.append((x, y))
        return False

    @agent.tool_plain(approval=needs_approval, policy_description="conditional approval")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")

    assert await session.status() == "completed"
    assert await session.result() == '{"add":0}'
    assert decisions == [(0, 0)]

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "ToolApprovalDecision" in event_types
    assert "ToolCallAwaitingApproval" not in event_types
    decision = next(event for _, event in events if event["type"] == "ToolApprovalDecision")
    assert decision["payload"]["approval_decision"] == {
        "gated": False,
        "policy": "callable",
        "callable_name": "needs_approval",
    }


async def test_callable_approval_true_pauses_then_resumes_tool_call_once():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    decisions: list[tuple[int, int]] = []
    agent = skrift.Agent(TestModel(), name="demo")

    def needs_approval(x: int, y: int) -> bool:
        decisions.append((x, y))
        return True

    @agent.tool_plain(approval=needs_approval, policy_description="conditional approval")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        async def wait_for_approval() -> None:
            while await session.status() != "awaiting_approval":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_approval(), timeout=1)
        state = await session.state()
        approval = state.pending_approvals[0]
        assert approval["approval_decision"]["gated"] is True
        assert decisions == [(0, 0)]

        await session.approve(approval["tool_call_id"], actor="ada", note="ok")

        assert await session.result() == '{"add":0}'
        assert decisions == [(0, 0)]
    finally:
        await runtime.stop()

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "ToolApprovalDecision" in event_types
    assert "ToolCallAwaitingApproval" in event_types
    assert "ToolCallCompleted" in event_types


async def test_callable_approval_supports_async_gate_on_context_tool():
    decisions: list[tuple[int, int]] = []
    agent = skrift.Agent(TestModel(), name="demo")

    async def needs_approval(x: int, y: int) -> bool:
        await asyncio.sleep(0)
        decisions.append((x, y))
        return False

    @agent.tool(approval=needs_approval, policy_description="conditional approval")
    def add(ctx: RunContext[None], x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")

    assert await session.status() == "completed"
    assert await session.result() == '{"add":0}'
    assert decisions == [(0, 0)]


async def test_callable_approval_can_use_context_deps_for_async_gate():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    gate_checks: list[tuple[str | None, str | None, str]] = []

    class EmailStore:
        async def threat_level(self, record_id: str) -> str:
            await asyncio.sleep(0)
            return "high"

    async def deps_factory(ctx):
        return EmailStore()

    agent = skrift.Agent(TestModel(), name="demo", deps_type=EmailStore, deps_factory=deps_factory)

    async def needs_approval(ctx: skrift.ApprovalContext, record_id: str) -> bool:
        threat_level = await ctx.deps.threat_level(record_id)
        gate_checks.append((ctx.session_id, ctx.tool_name, threat_level))
        return threat_level == "high"

    @agent.tool_plain(approval=needs_approval, idempotent=True)
    async def read_email(record_id: str) -> str:
        return f"body:{record_id}"

    session = await agent.run("read the email", actor="ada")
    await runtime.start()
    try:
        async def wait_for_approval() -> None:
            while await session.status() != "awaiting_approval":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_approval(), timeout=1)
        state = await session.state()
        approval = state.pending_approvals[0]
        assert approval["tool_name"] == "read_email"
        assert approval["approval_decision"] == {
            "gated": True,
            "policy": "callable",
            "callable_name": "needs_approval",
        }
        assert gate_checks == [(session.id, "read_email", "high")]

        await session.approve(approval["tool_call_id"], actor="ada", note="ok")

        assert await session.result() == '{"read_email":"body:a"}'
        assert gate_checks == [(session.id, "read_email", "high")]
    finally:
        await runtime.stop()


async def test_require_approval_pauses_context_tool_until_approved():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    attempts: list[str] = []
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool(idempotent=True)
    async def read_email(ctx: RunContext[None], record_id: str) -> str:
        attempts.append(record_id)
        await skrift.require_approval(
            ctx,
            reason="flagged_email",
            payload={"record_id": record_id, "threat_level": "high"},
        )
        return f"body:{record_id}"

    session = await agent.run("read the email", actor="ada")
    await runtime.start()
    try:
        async def wait_for_approval() -> None:
            while await session.status() != "awaiting_approval":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_approval(), timeout=1)
        state = await session.state()
        approval = state.pending_approvals[0]
        assert approval["approval_decision"] == {
            "gated": True,
            "policy": "runtime",
            "reason": "flagged_email",
        }
        assert approval["requesting_context"]["skrift_runtime_approval"] == {
            "reason": "flagged_email",
            "payload": {"record_id": "a", "threat_level": "high"},
        }
        assert attempts == ["a"]

        await session.approve(approval["tool_call_id"], actor="ada", note="ok")

        assert await session.result() == '{"read_email":"body:a"}'
        assert attempts == ["a", "a"]
    finally:
        await runtime.stop()


async def test_hitl_rejection_returns_denial_to_model():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(approval=True, policy_description="approval required")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        while await session.status() != "awaiting_approval":
            await asyncio.sleep(0.01)
        state = await session.state()
        tool_call_id = state.pending_approvals[0]["tool_call_id"]

        await session.reject(tool_call_id, actor="ada", reason="not allowed")

        assert await session.result() == '{"add":"not allowed"}'
    finally:
        await runtime.stop()

    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "ToolCallRejected" in event_types


async def test_hitl_rejection_with_payload_returns_structured_denial_to_model_and_audit():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(approval=True, policy_description="approval required")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        while await session.status() != "awaiting_approval":
            await asyncio.sleep(0.01)
        await runtime.stop()

        state = await session.state()
        tool_call_id = state.pending_approvals[0]["tool_call_id"]

        await session.reject(
            tool_call_id,
            actor="ada",
            reason="user redirected to draft",
            payload={"action": "saved_as_draft", "draft_id": "draft_123"},
        )

        state = await session.state()
        assert state.deferred_tool_results["approvals"][tool_call_id]["payload"] == {
            "action": "saved_as_draft",
            "draft_id": "draft_123",
        }

        await runtime.start()
        assert await session.result() == (
            '{"add":{"rejected":true,"reason":"user redirected to draft",'
            '"payload":{"action":"saved_as_draft","draft_id":"draft_123"}}}'
        )
    finally:
        await runtime.stop()

    audit = await skrift.audit_export(session.id)
    rejection = next(event for event in audit.events if event["type"] == "ToolCallRejected")
    assert rejection["payload"]["payload"] == {
        "action": "saved_as_draft",
        "draft_id": "draft_123",
    }


async def test_detached_tool_runs_in_tool_call_job_and_wakes_parent():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(detached=True, policy_description="run in a worker job")
    def add(x: int, y: int) -> int:
        return x + y

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        assert await session.result() == '{"add":0}'
    finally:
        await runtime.stop()

    state = await session.state()
    assert state.current_tool_execution is None
    events = await skrift.get_runtime().event_log.read(f"agents:run:{session.id}")
    event_types = [event["type"] for _, event in events]
    assert "ToolCallDispatched" in event_types
    assert "ToolCallCompleted" in event_types
    assert event_types[-1] == "AgentCompleted"


async def test_detached_tool_dlq_wakes_parent_with_error_result():
    runtime = skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(), name="demo")

    @agent.tool_plain(detached=True, policy_description="run in a worker job")
    def fail(x: int) -> int:
        raise RuntimeError("boom")

    session = await agent.run("use the tool", actor="ada")
    await runtime.start()
    try:
        output = await session.result()
    finally:
        await runtime.stop()

    assert "boom" in output
    entries = await runtime.inspect_dlq(job_type="agents.tool_call")
    assert len(entries) == 1


async def test_subagent_lineage_events_are_emitted_on_parent_stream():
    parent = skrift.Agent(TestModel(), name="parent")
    child = skrift.Agent(TestModel(custom_output_text="child-output"), name="child")

    @parent.tool_plain
    async def run_child() -> str:
        child_session = await child.run("child task", actor="ada")
        return await child_session.result()

    parent_session = await parent.run("use the tool", actor="ada")

    assert await parent_session.result() == '{"run_child":"child-output"}'
    parent_events = await skrift.get_runtime().event_log.read(f"agents:run:{parent_session.id}")
    parent_event_types = [event["type"] for _, event in parent_events]
    assert "SubAgentDispatched" in parent_event_types
    assert "SubAgentCompleted" in parent_event_types

    dispatched = next(event for _, event in parent_events if event["type"] == "SubAgentDispatched")
    child_session_id = dispatched["payload"]["child_session_id"]
    child_state = await load_runstate(child_session_id)
    assert child_state.parent_session_id == parent_session.id
    assert child_state.root_session_id == parent_session.id

    audit = await skrift.audit_export(parent_session.id)
    assert child_session_id in audit.lineage["included_session_ids"]
    assert any(
        event["session_stream_id"] == child_session_id and event["type"] == "AgentCompleted"
        for event in audit.events
    )


async def test_session_artifacts_include_lineage_by_default():
    parent = skrift.Agent(TestModel(), name="parent")
    child = skrift.Agent(TestModel(), name="child")

    @child.tool
    async def create_memory(ctx: RunContext[None]) -> str:
        await skrift.record_artifact(ctx, {"id": "child_mem"}, kind="memory")
        return "created"

    @parent.tool_plain
    async def run_child() -> str:
        child_session = await child.run("use the tool", actor="ada")
        return await child_session.result()

    parent_session = await parent.run("use the tool", actor="ada")

    await parent_session.result()
    assert await parent_session.artifacts(kind="memory") == [{"id": "child_mem"}]
    assert await parent_session.artifacts(kind="memory", include_lineage=False) == []


def test_detached_context_tools_fail_at_registration():
    agent = skrift.Agent(TestModel(), name="demo")

    with pytest.raises(
        NotImplementedError,
        match="detached=True is not yet supported for context tools",
    ):
        agent.tool(detached=True)


async def test_outbox_reconciler_drains_pending_entries():
    skrift.configure_workers(mode="in_process", queues=("agents",))
    agent = skrift.Agent(TestModel(custom_output_text="hello"), name="demo")
    session = await agent.run("hi", actor="ada")

    async def add_pending(state):
        from skrift.agents.state import append_event

        append_event(state, "SteerApplied", {"steer_id": "manual", "applied_at": "now", "position_in_history": 0})
        return state

    from skrift.agents.state import update_runstate

    await update_runstate(session.id, add_pending)
    assert (await load_runstate(session.id)).outbox

    drained = await drain_pending_outboxes()

    assert drained == [session.id]
    assert (await load_runstate(session.id)).outbox == []
