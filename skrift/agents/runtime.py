"""Worker handlers for Skrift agents."""

from __future__ import annotations

import inspect
import json
import logging
import traceback
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_core import PydanticSerializationError, to_jsonable_python

from skrift.agents.config import get_agents_config
from skrift.agents.context import reset_current_session_id, set_current_session_id
from skrift.agents.models import (
    AgentUsageRecord,
    AgentUsageTotals,
    AgentRunJob,
    AgentToolCallJob,
    ApprovalRejection,
    OutboxSubmit,
    ResumeContext,
    ToolDisplayContext,
    ToolDisplayMessage,
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
from skrift.workers import DeadLetterCause, PermanentFailure, WorkerContext, handler
from skrift.workers.registry import registry as worker_registry
from skrift.workers.models import DeadJobEntry, Pause, utcnow

logger = logging.getLogger(__name__)


class _RunnerStopped:
    pass


RUNNER_STOPPED = _RunnerStopped()


@dataclass(frozen=True)
class AgentIterResult:
    result: Any
    streamed_message_count: int
    usage: Any = None
    response: Any = None


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
        iter_result = await _drive_agent_iter(
            agent,
            payload.session_id,
            prompt,
            deps=deps,
            message_history=message_history,
            deferred_tool_results=deferred_tool_results,
            run_kwargs=run_kwargs,
        )
        if iter_result is RUNNER_STOPPED:
            return None
        result = iter_result.result
        streamed_message_count = iter_result.streamed_message_count
        if isinstance(result, Pause):
            return result
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
    unstreamed_messages = []
    try:
        result_messages = result.new_messages()
        new_messages = ModelMessagesTypeAdapter.dump_python(
            result_messages,
            mode="json",
        )
        unstreamed_messages = ModelMessagesTypeAdapter.dump_python(
            result_messages[streamed_message_count:],
            mode="json",
        )
    except Exception:
        new_messages = []
        unstreamed_messages = []

    if isinstance(output, DeferredToolRequests):
        if output.calls:
            call = output.calls[0]
            tool_job_id = uuid4().hex
            formatted_events = await _formatted_tool_events_from_messages(
                agent,
                payload.session_id,
                unstreamed_messages,
            )
            dispatch_payload = {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "args": call.args_as_dict(),
                "tool_job_id": tool_job_id,
            }
            display = await _tool_display_for_payload(
                agent,
                payload.session_id,
                "ToolCallStarted",
                dispatch_payload,
            )
            if display is not None:
                dispatch_payload["display"] = display

            async def await_detached_tool(runstate):
                _record_turn_usage(
                    runstate,
                    agent=agent,
                    context=context,
                    usage=iter_result.usage,
                    response=iter_result.response,
                )
                if new_messages:
                    runstate.messages.extend(
                        {"role": "model", "content": item} for item in new_messages
                    )
                for event_type, event_payload in formatted_events:
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
                append_event(runstate, "ToolCallDispatched", dispatch_payload)
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

        formatted_events = await _formatted_tool_events_from_messages(
            agent,
            payload.session_id,
            unstreamed_messages,
        )

        async def await_approval(runstate):
            runstate.status = "awaiting_approval"
            _record_turn_usage(
                runstate,
                agent=agent,
                context=context,
                usage=iter_result.usage,
                response=iter_result.response,
            )
            if new_messages:
                runstate.messages.extend(
                    {"role": "model", "content": item} for item in new_messages
                )
            for event_type, event_payload in formatted_events:
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

    formatted_events = await _formatted_tool_events_from_messages(
        agent,
        payload.session_id,
        unstreamed_messages,
    )

    async def complete(runstate):
        if runstate.terminal_at is not None:
            return runstate
        completed_at = utcnow()
        runstate.output = output
        if runstate.current_turn_id:
            runstate.turn_results[runstate.current_turn_id] = output
            if "output_type" in runstate.run_kwargs:
                runstate.turn_output_types[runstate.current_turn_id] = runstate.run_kwargs[
                    "output_type"
                ]
            else:
                runstate.turn_output_types.pop(runstate.current_turn_id, None)
        runstate.deferred_tool_results = {}
        runstate.current_tool_execution = None
        _record_turn_usage(
            runstate,
            agent=agent,
            context=context,
            usage=iter_result.usage,
            response=iter_result.response,
        )
        if new_messages:
            runstate.messages.extend({"role": "model", "content": item} for item in new_messages)
        for event_type, payload in formatted_events:
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
        discard_pending = entry.cause in {
            DeadLetterCause.PERMANENT_FAILURE,
            DeadLetterCause.POISON,
        }
        dropped_pending_messages = (
            len(runstate.pending_user_messages) if discard_pending else 0
        )
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
                "dropped_pending_messages": dropped_pending_messages,
            },
        )
        if discard_pending:
            runstate.pending_user_messages = []
        else:
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
            streamed_message_count = 0
            async for node in run:
                pause = await _runner_check_pass(session_id, node)
                if pause is not None:
                    return AgentIterResult(
                        result=pause,
                        streamed_message_count=streamed_message_count,
                        usage=run.usage(),
                        response=_latest_response(run),
                    )
                node_kind = type(node).__name__
                message_delta, streamed_message_count = _new_message_delta(
                    run,
                    streamed_message_count,
                )
                formatted_events = await _formatted_tool_events_from_messages(
                    agent,
                    session_id,
                    message_delta,
                )

                async def record_cursor(runstate):
                    runstate.cursor = {
                        "node_index": node_index,
                        "node_kind": node_kind,
                    }
                    for event_type, event_payload in formatted_events:
                        append_event(runstate, event_type, event_payload)
                    return runstate

                await update_runstate(session_id, record_cursor)
                await drain_outbox(session_id)
                node_index += 1
            return AgentIterResult(
                result=run.result,
                streamed_message_count=streamed_message_count,
                usage=run.usage(),
                response=_latest_response(run),
            )
    finally:
        reset_current_session_id(token)


def _record_turn_usage(
    runstate: Any,
    *,
    agent: Any,
    context: WorkerContext,
    usage: Any,
    response: Any,
) -> None:
    if usage is None or runstate.current_turn_id is None:
        return
    segment = _usage_totals_from_pydantic(usage)
    if not _usage_has_values(segment):
        return
    previous = runstate.turn_usage.get(runstate.current_turn_id)
    totals = _usage_totals_add(previous or AgentUsageTotals(), segment)
    record = AgentUsageRecord(
        **totals.model_dump(mode="json"),
        session_id=runstate.session_id,
        turn_id=runstate.current_turn_id,
        run_job_id=context.job.id,
        agent_name=runstate.agent_name,
        actor=runstate.created_by,
        root_session_id=runstate.root_session_id,
        parent_session_id=runstate.parent_session_id,
        model_name=_response_attr(response, "model_name"),
        configured_model=_configured_model(agent, runstate),
        provider_name=_response_attr(response, "provider_name"),
        provider_url=_response_attr(response, "provider_url"),
        metadata=_usage_metadata(response),
    )
    runstate.turn_usage[runstate.current_turn_id] = record
    runstate.usage_totals = _sum_usage_records(runstate.turn_usage.values())
    append_event(runstate, "AgentUsageRecorded", record.model_dump(mode="json"))


def _latest_response(run: Any) -> Any:
    try:
        return run.result.response if run.result is not None else None
    except Exception:
        try:
            messages = run.new_messages()
            for message in reversed(messages):
                if getattr(message, "kind", None) == "response":
                    return message
        except Exception:
            return None
    return None


def _usage_totals_from_pydantic(usage: Any) -> AgentUsageTotals:
    details = getattr(usage, "details", {}) or {}
    return AgentUsageTotals(
        requests=int(getattr(usage, "requests", 0) or 0),
        tool_calls=int(getattr(usage, "tool_calls", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        input_audio_tokens=int(getattr(usage, "input_audio_tokens", 0) or 0),
        cache_audio_read_tokens=int(getattr(usage, "cache_audio_read_tokens", 0) or 0),
        output_audio_tokens=int(getattr(usage, "output_audio_tokens", 0) or 0),
        details={str(key): int(value) for key, value in details.items()},
    )


def _usage_totals_add(left: AgentUsageTotals, right: AgentUsageTotals) -> AgentUsageTotals:
    details = dict(left.details)
    for key, value in right.details.items():
        details[key] = details.get(key, 0) + value
    return AgentUsageTotals(
        requests=left.requests + right.requests,
        tool_calls=left.tool_calls + right.tool_calls,
        input_tokens=left.input_tokens + right.input_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
        cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        input_audio_tokens=left.input_audio_tokens + right.input_audio_tokens,
        cache_audio_read_tokens=left.cache_audio_read_tokens + right.cache_audio_read_tokens,
        output_audio_tokens=left.output_audio_tokens + right.output_audio_tokens,
        details=details,
    )


def _sum_usage_records(records: Any) -> AgentUsageTotals:
    total = AgentUsageTotals()
    for record in records:
        total = _usage_totals_add(total, record)
    return total


def _usage_has_values(usage: AgentUsageTotals) -> bool:
    return any(
        (
            usage.requests,
            usage.tool_calls,
            usage.input_tokens,
            usage.cache_write_tokens,
            usage.cache_read_tokens,
            usage.output_tokens,
            usage.input_audio_tokens,
            usage.cache_audio_read_tokens,
            usage.output_audio_tokens,
            usage.details,
        )
    )


def _response_attr(response: Any, name: str) -> str | None:
    value = getattr(response, name, None) if response is not None else None
    return str(value) if value is not None else None


def _configured_model(agent: Any, runstate: Any) -> str | None:
    configured = runstate.run_kwargs.get("model")
    if configured is None:
        configured = getattr(agent, "model", None)
    return str(configured) if configured is not None else None


def _usage_metadata(response: Any) -> dict[str, Any]:
    if response is None:
        return {}
    metadata: dict[str, Any] = {}
    for key in ("provider_response_id", "finish_reason", "run_id", "conversation_id"):
        value = getattr(response, key, None)
        if value is not None:
            metadata[key] = str(value)
    provider_details = getattr(response, "provider_details", None)
    if provider_details:
        metadata["provider_details"] = provider_details
    response_metadata = getattr(response, "metadata", None)
    if response_metadata:
        metadata["response_metadata"] = response_metadata
    return metadata


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


def _new_message_delta(run: Any, emitted_count: int) -> tuple[list[dict[str, Any]], int]:
    try:
        messages = run.new_messages()
        if len(messages) <= emitted_count:
            return [], emitted_count
        delta = ModelMessagesTypeAdapter.dump_python(
            messages[emitted_count:],
            mode="json",
        )
        return delta, len(messages)
    except Exception:
        return [], emitted_count


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
                            "args": _coerce_tool_args(part.get("args")),
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
                                "tool_name": part.get("tool_name") or "",
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
                                "tool_name": part.get("tool_name") or "",
                                "exception_type": "ToolError",
                                "exception_message": str(part.get("content")),
                                "traceback": "",
                                "duration_ms": 0,
                            },
                        )
                    )
    return events


async def _formatted_tool_events_from_messages(
    agent: Any,
    session_id: str,
    messages: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    events = _tool_events_from_messages(messages)
    args_by_call: dict[str, dict[str, Any]] = {}
    tool_by_call: dict[str, str] = {}
    formatted: list[tuple[str, dict[str, Any]]] = []
    for event_type, event_payload in events:
        tool_call_id = str(event_payload.get("tool_call_id") or "")
        if event_type == "ToolCallStarted":
            args_by_call[tool_call_id] = dict(event_payload.get("args") or {})
            tool_by_call[tool_call_id] = str(event_payload.get("tool_name") or "")
        elif tool_call_id:
            if not event_payload.get("tool_name") and tool_call_id in tool_by_call:
                event_payload["tool_name"] = tool_by_call[tool_call_id]
            if tool_call_id in args_by_call:
                event_payload.setdefault("args", args_by_call[tool_call_id])
        display = await _tool_display_for_payload(
            agent,
            session_id,
            event_type,
            event_payload,
        )
        if display is not None:
            event_payload["display"] = display
        formatted.append((event_type, event_payload))
    return formatted


async def _tool_display_for_payload(
    agent: Any,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    formatter_kind = {
        "ToolCallStarted": "called",
        "ToolCallDispatched": "called",
        "ToolCallCompleted": "returned",
        "ToolCallErrored": "errored",
    }.get(event_type)
    if formatter_kind is None:
        return None
    tool_name = str(payload.get("tool_name") or "")
    formatters = getattr(agent, "_tool_formatters", {}).get(tool_name)
    formatter = getattr(formatters, formatter_kind, None) if formatters else None
    if formatter is None:
        return None
    context = ToolDisplayContext(
        session_id=session_id,
        tool_call_id=str(payload.get("tool_call_id") or ""),
        tool_name=tool_name,
        args=dict(payload.get("args") or {}),
        result=payload.get("result"),
        error=_error_payload(payload) if formatter_kind == "errored" else None,
    )
    try:
        value = formatter(context)
        if inspect.isawaitable(value):
            value = await value
        return _coerce_tool_display(value, event_type)
    except Exception:
        logger.exception("Tool display formatter failed for %s", tool_name)
        return _fallback_tool_display(tool_name, event_type)


def _error_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "exception_type": payload.get("exception_type") or "",
        "exception_message": payload.get("exception_message") or "",
        "traceback": payload.get("traceback") or "",
    }


def _coerce_tool_display(value: Any, event_type: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, ToolDisplayMessage):
        message = value
    elif isinstance(value, str):
        message = ToolDisplayMessage(message=value, level=_display_level(event_type))
    elif isinstance(value, dict):
        message = ToolDisplayMessage.model_validate(value)
    else:
        message = ToolDisplayMessage(message=str(value), level=_display_level(event_type))
    try:
        return to_jsonable_python(message.model_dump(mode="json"))
    except PydanticSerializationError:
        return ToolDisplayMessage(
            message=message.message,
            level=message.level,
        ).model_dump(mode="json")


def _fallback_tool_display(tool_name: str, event_type: str) -> dict[str, Any]:
    action = {
        "ToolCallStarted": "Called",
        "ToolCallDispatched": "Called",
        "ToolCallCompleted": "Completed",
        "ToolCallErrored": "Failed",
    }.get(event_type, "Updated")
    return ToolDisplayMessage(
        message=f"{action} {tool_name or 'tool'}",
        level=_display_level(event_type),
    ).model_dump(mode="json")


def _display_level(event_type: str) -> str:
    if event_type == "ToolCallCompleted":
        return "success"
    if event_type == "ToolCallErrored":
        return "error"
    return "info"


def _coerce_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"INVALID_JSON": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"INVALID_JSON": raw}
    return {}


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
