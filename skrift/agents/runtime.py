"""Worker handlers for Skrift agents."""

from __future__ import annotations

import traceback
import inspect
from typing import Any
from uuid import uuid4

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.messages import ModelMessagesTypeAdapter

from skrift.agents.config import get_agents_config
from skrift.agents.context import reset_current_session_id, set_current_session_id
from skrift.agents.models import (
    AgentRunJob,
    AgentToolCallJob,
    ApprovalRejection,
    OutboxSubmit,
    ResumeContext,
    ToolExecutionState,
)
from skrift.agents.registry import registry
from skrift.agents.state import (
    actor_payload,
    append_event,
    append_submit,
    append_wake,
    drain_outbox,
    load_runstate,
    update_runstate,
)
from skrift.agents.turns import decode_turn_kwargs
from skrift.workers import PermanentFailure, WorkerContext, handler
from skrift.workers.registry import registry as worker_registry
from skrift.workers.models import DeadJobEntry, Pause, utcnow


class _RunnerStopped:
    pass


RUNNER_STOPPED = _RunnerStopped()


@handler("agents.run", queue="agents")
async def agents_run_handler(payload: AgentRunJob, context: WorkerContext) -> Any:
    await drain_outbox(payload.session_id)
    state = await load_runstate(payload.session_id)
    if state is None:
        raise PermanentFailure(f"Unknown agent session {payload.session_id!r}")
    if context.job.id != state.current_run_job_id:
        return None
    if state.status == "paused":
        return Pause(state={"resume": "manual"})
    if state.status == "cancelled":
        await _finalize_cancelled(payload.session_id, state.status)
        return None
    if state.status in {"completed", "failed"}:
        return None

    definition = registry.get(payload.agent_name)
    agent = definition.agent
    prior_status = state.status

    async def start(runstate):
        runstate.status = "running"
        now = utcnow()
        if runstate.started_at is None:
            runstate.started_at = now
            append_event(
                runstate,
                "AgentStarted",
                {
                    "agent_name": runstate.agent_name,
                    "agent_definition": agent.definition_snapshot(),
                    "input_message": runstate.messages[0]["content"] if runstate.messages else None,
                    "actor": actor_payload(runstate.created_by),
                    "parent_session_id": runstate.parent_session_id,
                    "root_session_id": runstate.root_session_id,
                    "dispatch_kind": "queued",
                },
            )
        else:
            append_event(
                runstate,
                "AgentResumed",
                {
                    "resumed_at": now.isoformat(),
                    "prior_status": prior_status,
                    "actor": None,
                },
            )
        return runstate

    await update_runstate(payload.session_id, start)
    await drain_outbox(payload.session_id)

    await _apply_pending_steers(payload.session_id)
    state = await load_runstate(payload.session_id)
    if state is None:
        raise PermanentFailure(f"Unknown agent session {payload.session_id!r}")

    run_kwargs = decode_turn_kwargs(state.run_kwargs)
    deps = run_kwargs.pop("deps", None)
    if definition.deps_factory is not None:
        deps = definition.deps_factory(
            ResumeContext(
                session_id=state.session_id,
                deps_ref=state.deps_ref,
                metadata={"worker_job_id": context.job.id},
            )
        )
        if inspect.isawaitable(deps):
            deps = await deps
    deferred_tool_results = _deferred_tool_results(state)
    prompt = None if deferred_tool_results is not None else _latest_user_prompt(state)
    message_history = _message_history(state)
    initial_message_history = run_kwargs.pop("message_history", None)
    if initial_message_history:
        message_history = [*initial_message_history, *message_history]
    run_kwargs.pop("deferred_tool_results", None)
    try:
        result = await _drive_agent_iter(
            agent,
            payload.session_id,
            prompt,
            deps=deps,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
            run_kwargs=run_kwargs,
        )
        if isinstance(result, Pause):
            return result
        if result is RUNNER_STOPPED:
            return None
    except Exception as exc:
        async def fail(runstate):
            if runstate.terminal_at is not None:
                return runstate
            runstate.status = "failed"
            runstate.terminal_at = utcnow()
            runstate.current_run_job_id = None
            runstate.error = {
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": traceback.format_exc(),
            }
            if runstate.current_turn_id:
                runstate.turn_errors[runstate.current_turn_id] = runstate.error
            append_event(
                runstate,
                "AgentFailed",
                {
                    "cause": "exception",
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "traceback": traceback.format_exc(),
                    "failed_at": runstate.terminal_at.isoformat(),
                },
            )
            _activate_next_pending_turn(runstate)
            return runstate

        failed_state = await update_runstate(payload.session_id, fail)
        await drain_outbox(payload.session_id)
        if failed_state.status == "failed":
            await _emit_subagent_completed(payload.session_id, "failed")
        raise

    output = getattr(result, "output", result)
    new_messages = []
    try:
        new_messages = ModelMessagesTypeAdapter.dump_python(
            result.new_messages(),
            mode="json",
        )
    except Exception:
        new_messages = []

    if isinstance(output, DeferredToolRequests):
        if output.calls:
            call = output.calls[0]
            tool_job_id = uuid4().hex

            async def await_detached_tool(runstate):
                if new_messages:
                    runstate.messages.extend(
                        {"role": "model", "content": item} for item in new_messages
                    )
                for event_type, event_payload in _tool_events_from_messages(new_messages):
                    append_event(runstate, event_type, event_payload)
                runstate.current_tool_execution = ToolExecutionState(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    args=call.args_as_dict(),
                    status="executing",
                    idempotent=agent._tool_policies.get(call.tool_name).idempotent
                    if call.tool_name in agent._tool_policies
                    else False,
                    approval_on_retry=agent._tool_policies.get(call.tool_name).approval_on_retry
                    if call.tool_name in agent._tool_policies
                    else False,
                    detached_tool_job_id=tool_job_id,
                )
                append_event(
                    runstate,
                    "ToolCallDispatched",
                    {
                        "tool_call_id": call.tool_call_id,
                        "tool_name": call.tool_name,
                        "args": call.args_as_dict(),
                        "tool_job_id": tool_job_id,
                    },
                )
                runstate.outbox.append(
                    OutboxSubmit(
                        job_type="agents.tool_call",
                        payload=AgentToolCallJob(
                            session_id=payload.session_id,
                            tool_call_id=call.tool_call_id,
                        ).model_dump(mode="json"),
                        queue=get_agents_config().tool_call_queue,
                        job_id=tool_job_id,
                    )
                )
                return runstate

            await update_runstate(payload.session_id, await_detached_tool)
            await drain_outbox(payload.session_id)
            return Pause(state={"detached_tool_job_id": tool_job_id})

        async def await_approval(runstate):
            runstate.status = "awaiting_approval"
            if new_messages:
                runstate.messages.extend(
                    {"role": "model", "content": item} for item in new_messages
                )
            for event_type, event_payload in _tool_events_from_messages(new_messages):
                append_event(runstate, event_type, event_payload)
            for call in output.approvals:
                metadata = output.metadata.get(call.tool_call_id, {})
                approval = {
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name,
                    "args": call.args_as_dict(),
                    "requesting_context": metadata,
                }
                approval_decision = metadata.get("skrift_approval_decision")
                if approval_decision is not None:
                    approval["approval_decision"] = approval_decision
                if not any(
                    existing.get("tool_call_id") == call.tool_call_id
                    for existing in runstate.pending_approvals
                ):
                    runstate.pending_approvals.append(approval)
                append_event(runstate, "ToolCallAwaitingApproval", approval)
            return runstate

        await update_runstate(payload.session_id, await_approval)
        await drain_outbox(payload.session_id)
        return Pause(
            state={
                "awaiting_approval_ids": [
                    call.tool_call_id for call in output.approvals
                ]
            }
        )

    async def complete(runstate):
        if runstate.terminal_at is not None:
            return runstate
        completed_at = utcnow()
        runstate.output = output
        if runstate.current_turn_id:
            runstate.turn_results[runstate.current_turn_id] = output
        runstate.deferred_tool_results = {}
        runstate.current_tool_execution = None
        if new_messages:
            runstate.messages.extend({"role": "model", "content": item} for item in new_messages)
        for event_type, payload in _tool_events_from_messages(new_messages):
            append_event(runstate, event_type, payload)
        append_event(
            runstate,
            "AssistantMessageCompleted",
            {"message": output, "model": str(getattr(agent, "model", ""))},
        )
        append_event(
            runstate,
            "AgentCompleted",
            {"output": output, "completed_at": completed_at.isoformat()},
        )
        if _activate_next_pending_turn(runstate):
            return runstate
        runstate.status = "completed"
        runstate.terminal_at = completed_at
        runstate.current_run_job_id = None
        return runstate

    completed_state = await update_runstate(payload.session_id, complete)
    await drain_outbox(payload.session_id)
    if completed_state.status == "completed":
        await _emit_subagent_completed(payload.session_id, "completed")
    return output


@agents_run_handler.on_dead
async def agents_run_dead(entry: DeadJobEntry) -> None:
    session_id = entry.job.payload.get("session_id")
    if not session_id:
        return

    async def finalize(runstate):
        if entry.job.id != runstate.current_run_job_id:
            return runstate
        if runstate.terminal_at is not None:
            return runstate
        runstate.status = "failed"
        runstate.terminal_at = utcnow()
        runstate.current_run_job_id = None
        latest = entry.attempts[-1] if entry.attempts else None
        runstate.error = {
            "exception_type": latest.exception_type if latest else "",
            "exception_message": latest.error if latest else entry.latest_error,
            "traceback": latest.traceback if latest else "",
        }
        if runstate.current_turn_id:
            runstate.turn_errors[runstate.current_turn_id] = runstate.error
        append_event(
            runstate,
            "AgentFailed",
            {
                "cause": entry.cause.value,
                "exception_type": runstate.error["exception_type"],
                "exception_message": runstate.error["exception_message"],
                "traceback": runstate.error["traceback"],
                "failed_at": runstate.terminal_at.isoformat(),
            },
        )
        _activate_next_pending_turn(runstate)
        return runstate

    failed_state = await update_runstate(session_id, finalize)
    await drain_outbox(session_id)
    if failed_state.status == "failed":
        await _emit_subagent_completed(session_id, "failed")


@handler("agents.tool_call", queue="agents")
async def agents_tool_call_handler(payload: AgentToolCallJob, context: WorkerContext) -> None:
    await drain_outbox(payload.session_id)
    state = await load_runstate(payload.session_id)
    if state is None:
        raise PermanentFailure(f"Unknown agent session {payload.session_id!r}")
    execution = state.current_tool_execution
    if (
        execution is None
        or execution.tool_call_id != payload.tool_call_id
        or execution.detached_tool_job_id != context.job.id
    ):
        return None
    definition = registry.get(state.agent_name)
    func = definition.agent._detached_tools.get(execution.tool_name)
    if func is None:
        raise PermanentFailure(f"No detached tool registered for {execution.tool_name!r}")

    token = set_current_session_id(payload.session_id)
    try:
        result = func(**execution.args)
        if inspect.isawaitable(result):
            result = await result
    finally:
        reset_current_session_id(token)

    async def store_result(runstate):
        current = runstate.current_tool_execution
        if current is None or current.tool_call_id != payload.tool_call_id:
            return runstate
        current.status = "completed"
        current.result = result
        runstate.deferred_tool_results.setdefault("calls", {})[payload.tool_call_id] = result
        if runstate.current_run_job_id:
            append_wake(runstate, runstate.current_run_job_id)
        return runstate

    await update_runstate(payload.session_id, store_result)
    await drain_outbox(payload.session_id)
    return None


@agents_tool_call_handler.on_dead
async def agents_tool_call_dead(entry: DeadJobEntry) -> None:
    session_id = entry.job.payload.get("session_id")
    tool_call_id = entry.job.payload.get("tool_call_id")
    if not session_id or not tool_call_id:
        return

    async def finalize(runstate):
        current = runstate.current_tool_execution
        if (
            current is None
            or current.tool_call_id != tool_call_id
            or current.detached_tool_job_id != entry.job.id
            or current.status in {"completed", "errored"}
        ):
            return runstate
        latest = entry.attempts[-1] if entry.attempts else None
        current.status = "errored"
        current.error = {
            "exception_type": latest.exception_type if latest else "",
            "exception_message": latest.error if latest else entry.latest_error,
            "traceback": latest.traceback if latest else "",
        }
        runstate.deferred_tool_results.setdefault("calls", {})[tool_call_id] = {
            "error": current.error,
        }
        if runstate.current_run_job_id:
            append_wake(runstate, runstate.current_run_job_id)
        return runstate

    await update_runstate(session_id, finalize)
    await drain_outbox(session_id)


async def _finalize_cancelled(session_id: str, reached_from_status: str) -> None:
    async def finalize(runstate):
        if runstate.terminal_at is not None:
            return runstate
        runstate.terminal_at = utcnow()
        runstate.current_run_job_id = None
        append_event(
            runstate,
            "AgentCancelled",
            {
                "cancelled_at": runstate.terminal_at.isoformat(),
                "reached_from_status": reached_from_status,
            },
        )
        return runstate

    await update_runstate(session_id, finalize)
    await drain_outbox(session_id)
    await _emit_subagent_completed(session_id, "cancelled")


async def _emit_subagent_completed(session_id: str, terminal_status: str) -> None:
    state = await load_runstate(session_id)
    if state is None or not state.parent_session_id:
        return

    async def emit(parent_state):
        append_event(
            parent_state,
            "SubAgentCompleted",
            {
                "child_session_id": session_id,
                "terminal_status": terminal_status,
                "child_terminal_at": state.terminal_at.isoformat()
                if state.terminal_at
                else None,
            },
        )
        return parent_state

    await update_runstate(state.parent_session_id, emit)
    await drain_outbox(state.parent_session_id)


def _latest_user_prompt(state) -> Any:
    for message in reversed(state.messages):
        if message.get("role") == "user":
            return message.get("content")
    return None


async def _drive_agent_iter(
    agent: Any,
    session_id: str,
    prompt: Any,
    *,
    deps: Any,
    message_history: list[Any],
    deferred_tool_results: DeferredToolResults | None,
    run_kwargs: dict[str, Any],
) -> Any:
    token = set_current_session_id(session_id)
    try:
        base_output_type = run_kwargs.pop("output_type", getattr(agent, "_output_type", str))
        if base_output_type is None:
            base_output_type = getattr(agent, "_output_type", str)
        output_type = _durable_output_type(base_output_type)
        async with agent._iter_pydantic(
            prompt,
            deps=deps,
            message_history=message_history or None,
            deferred_tool_results=deferred_tool_results,
            output_type=output_type,
            **run_kwargs,
        ) as run:
            node_index = 0
            async for node in run:
                pause = await _runner_check_pass(session_id, node)
                if pause is not None:
                    return pause
                node_kind = type(node).__name__

                async def record_cursor(runstate):
                    runstate.cursor = {
                        "node_index": node_index,
                        "node_kind": node_kind,
                    }
                    return runstate

                await update_runstate(session_id, record_cursor)
                await drain_outbox(session_id)
                node_index += 1
            return run.result
    finally:
        reset_current_session_id(token)


async def _runner_check_pass(session_id: str, node: Any) -> Pause | _RunnerStopped | None:
    await drain_outbox(session_id)
    state = await load_runstate(session_id)
    if state is None:
        raise PermanentFailure(f"Unknown agent session {session_id!r}")
    if state.status == "cancelled":
        await _finalize_cancelled(session_id, "running")
        return RUNNER_STOPPED
    if state.status == "paused":
        return Pause(state={"resume": "manual"})
    if type(node).__name__ == "ModelRequestNode":
        await _apply_pending_steers(session_id)
    return None


def _activate_next_pending_turn(runstate: Any) -> bool:
    if not runstate.pending_user_messages:
        return False
    turn = runstate.pending_user_messages.pop(0)
    runstate.messages.append(
        {"role": "user", "content": turn.get("message"), "turn_id": turn.get("turn_id")}
    )
    runstate.status = "queued"
    runstate.terminal_at = None
    runstate.error = None
    runstate.pending_approvals = []
    runstate.deferred_tool_results = {}
    runstate.current_tool_execution = None
    runstate.paused_at = None
    runstate.status_before_pause = None
    runstate.current_turn_id = turn.get("turn_id")
    runstate.run_kwargs = dict(turn.get("run_kwargs") or {})
    runstate.current_run_job_id = uuid4().hex
    append_event(
        runstate,
        "UserMessageActivated",
        {
            "message": turn.get("message"),
            "actor": turn.get("actor"),
            "turn_id": turn.get("turn_id"),
            "turn_config": turn.get("run_kwargs") or {},
            "activated_at": utcnow().isoformat(),
            "remaining_pending_messages": len(runstate.pending_user_messages),
        },
    )
    append_submit(runstate, runstate.current_run_job_id)
    return True


def _durable_output_type(output_type: Any) -> Any:
    if output_type is None:
        output_types = []
    elif isinstance(output_type, list):
        output_types = list(output_type)
    elif isinstance(output_type, tuple):
        output_types = list(output_type)
    else:
        output_types = [output_type]
    if not any(item is DeferredToolRequests for item in output_types):
        output_types.append(DeferredToolRequests)
    return output_types


async def _apply_pending_steers(session_id: str) -> None:
    state = await load_runstate(session_id)
    if state is None or not state.pending_steers:
        return

    async def apply(runstate):
        steers = list(runstate.pending_steers)
        runstate.pending_steers = []
        for steer in steers:
            content = f"{get_agents_config().steer_prefix}{steer.text}"
            runstate.messages.append({"role": steer.role, "content": content})
            append_event(
                runstate,
                "SteerApplied",
                {
                    "steer_id": steer.steer_id,
                    "applied_at": utcnow().isoformat(),
                    "position_in_history": len(runstate.messages) - 1,
                },
            )
        return runstate

    await update_runstate(session_id, apply)
    await drain_outbox(session_id)


def _message_history(state) -> list[Any]:
    raw_messages = [
        message.get("content")
        for message in state.messages
        if message.get("role") == "model" and isinstance(message.get("content"), dict)
    ]
    if not raw_messages:
        return []
    return ModelMessagesTypeAdapter.validate_python(raw_messages)


def _deferred_tool_results(state) -> DeferredToolResults | None:
    approvals = state.deferred_tool_results.get("approvals", {})
    calls = state.deferred_tool_results.get("calls", {})
    if not approvals and not calls:
        return None
    resolved = {}
    for tool_call_id, decision in approvals.items():
        if decision.get("approved"):
            resolved[tool_call_id] = True
        else:
            reason = decision.get("message") or "The tool call was denied."
            if "payload" in decision:
                denial = ApprovalRejection(
                    reason=reason,
                    payload=decision.get("payload"),
                ).model_dump(mode="json")
            else:
                denial = reason
            resolved[tool_call_id] = ToolDenied(denial)
    return DeferredToolResults(approvals=resolved, calls=calls)


def _tool_events_from_messages(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for message in messages:
        for part in message.get("parts", []):
            kind = part.get("part_kind")
            if kind == "tool-call":
                events.append(
                    (
                        "ToolCallStarted",
                        {
                            "tool_call_id": part.get("tool_call_id") or "",
                            "tool_name": part.get("tool_name") or "",
                            "args": part.get("args") or {},
                        },
                    )
                )
                events.append(
                    (
                        "ToolCallExecuting",
                        {"tool_call_id": part.get("tool_call_id") or ""},
                    )
                )
            elif kind == "tool-return":
                if part.get("outcome", "success") == "success":
                    events.append(
                        (
                            "ToolCallCompleted",
                            {
                                "tool_call_id": part.get("tool_call_id") or "",
                                "result": part.get("content"),
                                "duration_ms": 0,
                            },
                        )
                    )
                else:
                    events.append(
                        (
                            "ToolCallErrored",
                            {
                                "tool_call_id": part.get("tool_call_id") or "",
                                "exception_type": "ToolError",
                                "exception_message": str(part.get("content")),
                                "traceback": "",
                                "duration_ms": 0,
                            },
                        )
                    )
    return events


def register_agent_handlers() -> None:
    """Re-register agent handlers after tests or hosts clear the worker registry."""

    try:
        worker_registry.get("agents.run")
    except KeyError:
        worker_registry.register(
            "agents.run",
            agents_run_handler,
            payload_model=AgentRunJob,
            queue="agents",
        )
        worker_registry.set_dead_callback("agents.run", agents_run_dead)
    try:
        worker_registry.get("agents.tool_call")
    except KeyError:
        worker_registry.register(
            "agents.tool_call",
            agents_tool_call_handler,
            payload_model=AgentToolCallJob,
            queue="agents",
        )
        worker_registry.set_dead_callback("agents.tool_call", agents_tool_call_dead)
