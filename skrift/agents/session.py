"""Session API for durable agent runs."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator
from uuid import uuid4

from skrift.agents.blob import dereference_blob_refs
from skrift.agents.context import resolve_actor
from skrift.agents.models import Actor, RunState, Steer
from skrift.agents.turns import normalize_turn_kwargs
from skrift.agents.state import (
    actor_payload,
    append_event,
    append_submit,
    append_wake,
    drain_outbox,
    load_runstate,
    stream_name,
    update_runstate,
)
from skrift.workers import get_runtime
from skrift.workers.models import utcnow


TERMINAL = {"completed", "failed", "cancelled"}


class AgentSessionError(RuntimeError):
    """Raised for invalid agent session operations."""


class Session:
    """Handle for a durable agent run."""

    def __init__(self, session_id: str) -> None:
        self.id = session_id

    async def state(self) -> RunState:
        await drain_outbox(self.id)
        state = await load_runstate(self.id)
        if state is None:
            raise KeyError(f"Unknown agent session {self.id!r}")
        return state

    async def status(self) -> str:
        return (await self.state()).status

    async def messages(self) -> list[dict[str, Any]]:
        return list((await self.state()).messages)

    async def lineage(self) -> dict[str, str | None]:
        state = await self.state()
        return {
            "parent_session_id": state.parent_session_id,
            "root_session_id": state.root_session_id,
        }

    async def send(
        self,
        message: Any,
        *,
        actor: Actor | dict | str | None = None,
        **kwargs: Any,
    ) -> str:
        resolved = resolve_actor(actor)
        job_id = uuid4().hex
        turn_id = uuid4().hex
        run_kwargs = normalize_turn_kwargs(kwargs)

        async def mutate(state: RunState) -> RunState:
            queued_for_later = False
            submit_job_id: str | None = None
            payload = {
                "turn_id": turn_id,
                "message": message,
                "actor": actor_payload(resolved),
                "run_kwargs": run_kwargs,
                "submitted_at": utcnow().isoformat(),
            }
            if state.status == "completed" or state.status in {"failed", "cancelled"}:
                state.messages.append({"role": "user", "content": message, "turn_id": turn_id})
                state.current_run_job_id = job_id
                state.current_turn_id = turn_id
                state.status = "queued"
                state.terminal_at = None
                state.error = None
                state.pending_approvals = []
                state.deferred_tool_results = {}
                state.current_tool_execution = None
                state.paused_at = None
                state.status_before_pause = None
                state.run_kwargs = run_kwargs
                submit_job_id = job_id
            else:
                queued_for_later = True
                state.pending_user_messages.append(payload)
                if state.status == "awaiting_approval" and state.pending_approvals:
                    approvals = state.deferred_tool_results.setdefault("approvals", {})
                    for approval in state.pending_approvals:
                        tool_call_id = approval.get("tool_call_id")
                        if not tool_call_id:
                            continue
                        approvals[tool_call_id] = {
                            "approved": False,
                            "message": "Cancelled because a new user message was received.",
                        }
                        append_event(
                            state,
                            "ToolCallRejected",
                            {
                                "tool_call_id": tool_call_id,
                                "actor": actor_payload(resolved),
                                "decided_at": utcnow().isoformat(),
                                "reason": "Cancelled because a new user message was received.",
                            },
                        )
                    state.pending_approvals = []
                    state.status = "queued"
                    if state.current_run_job_id:
                        append_wake(state, state.current_run_job_id)
                elif state.status == "paused":
                    state.status = "queued"
                    state.paused_at = None
                    state.status_before_pause = None
                    if state.current_run_job_id:
                        append_wake(state, state.current_run_job_id)
                    else:
                        state.current_run_job_id = job_id
                        state.current_turn_id = turn_id
                        state.run_kwargs = run_kwargs
                        submit_job_id = job_id
            append_event(
                state,
                "UserMessageReceived",
                {
                    "message": message,
                    "actor": actor_payload(resolved),
                    "turn_id": turn_id,
                    "turn_index": len(state.messages) + len(state.pending_user_messages) - 1,
                    "queued": queued_for_later,
                    "turn_config": run_kwargs,
                },
            )
            if submit_job_id is not None:
                append_submit(state, submit_job_id)
            return state

        await update_runstate(self.id, mutate)
        await drain_outbox(self.id)
        return turn_id

    async def steer(
        self,
        text: str,
        *,
        role: str = "user",
        actor: Actor | dict | str | None = None,
    ) -> None:
        resolved = resolve_actor(actor)
        steer = Steer(text=text, role=role, actor=resolved)

        async def mutate(state: RunState) -> RunState:
            if state.status in TERMINAL:
                raise AgentSessionError("Cannot steer a terminal agent session")
            state.pending_steers.append(steer)
            append_event(
                state,
                "SteerInjected",
                {
                    "steer_id": steer.steer_id,
                    "text": text,
                    "role": role,
                    "actor": actor_payload(resolved),
                    "submitted_at": steer.submitted_at.isoformat(),
                },
            )
            return state

        await update_runstate(self.id, mutate)
        await drain_outbox(self.id)

    async def cancel(self, *, actor: Actor | dict | str | None = None) -> None:
        resolved = resolve_actor(actor)
        run_job_id: str | None = None

        async def mutate(state: RunState) -> RunState:
            nonlocal run_job_id
            if state.terminal_at is not None:
                return state
            run_job_id = state.current_run_job_id
            prior_status = state.status
            state.status = "cancelled"
            append_event(
                state,
                "AgentCancellationRequested",
                {"actor": actor_payload(resolved), "requested_at": utcnow().isoformat()},
            )
            if run_job_id is None:
                state.terminal_at = utcnow()
                append_event(
                    state,
                    "AgentCancelled",
                    {"cancelled_at": state.terminal_at.isoformat(), "reached_from_status": prior_status},
                )
            return state

        await update_runstate(self.id, mutate)
        await drain_outbox(self.id)
        if run_job_id is not None:
            handle = get_runtime().handle(run_job_id)
            cancelled = await handle.cancel()
            if cancelled:
                async def finalize(state: RunState) -> RunState:
                    if state.terminal_at is not None:
                        return state
                    state.terminal_at = utcnow()
                    state.current_run_job_id = None
                    append_event(
                        state,
                        "AgentCancelled",
                        {
                            "cancelled_at": state.terminal_at.isoformat(),
                            "reached_from_status": "queued",
                        },
                    )
                    return state

                await update_runstate(self.id, finalize)
            else:
                async def wake(state: RunState) -> RunState:
                    if state.current_run_job_id:
                        append_wake(state, state.current_run_job_id)
                    return state

                await update_runstate(self.id, wake)
            await drain_outbox(self.id)

    async def pause(self, *, actor: Actor | dict | str | None = None) -> None:
        resolved = resolve_actor(actor)

        async def mutate(state: RunState) -> RunState:
            if state.status == "paused":
                return state
            if state.status not in {"queued", "running", "awaiting_approval"}:
                raise AgentSessionError(f"Cannot pause session in status {state.status!r}")
            prior = state.status
            state.status_before_pause = prior
            state.status = "paused"
            state.paused_at = utcnow()
            append_event(
                state,
                "AgentPaused",
                {
                    "paused_at": state.paused_at.isoformat(),
                    "prior_status": prior,
                    "actor": actor_payload(resolved),
                },
            )
            return state

        await update_runstate(self.id, mutate)
        await drain_outbox(self.id)

    async def resume(self, *, actor: Actor | dict | str | None = None) -> None:
        resolved = resolve_actor(actor)
        job_id = uuid4().hex

        async def mutate(state: RunState) -> RunState:
            if state.status != "paused":
                raise AgentSessionError("Can only resume a paused session")
            prior = state.status_before_pause
            state.status = "awaiting_approval" if prior == "awaiting_approval" else "queued"
            state.paused_at = None
            state.status_before_pause = None
            append_event(
                state,
                "AgentResumed",
                {
                    "resumed_at": utcnow().isoformat(),
                    "prior_status": "paused",
                    "actor": actor_payload(resolved),
                },
            )
            if state.current_run_job_id:
                append_wake(state, state.current_run_job_id)
            else:
                state.current_run_job_id = job_id
                append_submit(state, job_id)
            return state

        await update_runstate(self.id, mutate)
        await drain_outbox(self.id)

    async def approve(self, tool_call_id: str, *, actor: Actor | dict | str | None = None, note: str | None = None) -> None:
        await self._decision(tool_call_id, approved=True, actor=actor, note=note)

    async def reject(self, tool_call_id: str, *, actor: Actor | dict | str | None = None, reason: str) -> None:
        await self._decision(tool_call_id, approved=False, actor=actor, note=reason)

    async def _decision(
        self,
        tool_call_id: str,
        *,
        approved: bool,
        actor: Actor | dict | str | None,
        note: str | None,
    ) -> None:
        resolved = resolve_actor(actor)
        event_type = "ToolCallApproved" if approved else "ToolCallRejected"

        async def mutate(state: RunState) -> RunState:
            if not any(item.get("tool_call_id") == tool_call_id for item in state.pending_approvals):
                raise AgentSessionError(f"No pending approval for tool call {tool_call_id!r}")
            state.pending_approvals = [
                item for item in state.pending_approvals if item.get("tool_call_id") != tool_call_id
            ]
            state.deferred_tool_results.setdefault("approvals", {})[tool_call_id] = {
                "approved": approved,
                "message": note,
            }
            state.status = "queued"
            payload = {
                "tool_call_id": tool_call_id,
                "actor": actor_payload(resolved),
                "decided_at": utcnow().isoformat(),
            }
            payload["note" if approved else "reason"] = note
            append_event(state, event_type, payload)
            if state.current_run_job_id:
                append_wake(state, state.current_run_job_id)
            return state

        await update_runstate(self.id, mutate)
        await drain_outbox(self.id)

    def __aiter__(self) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        return self._events()

    async def _events(self) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        await drain_outbox(self.id)
        runtime = get_runtime()
        cursor = 0
        for position, event in await runtime.event_log.read(stream_name(self.id)):
            cursor = position + 1
            yield position, await dereference_blob_refs(event)
        async for event in runtime.event_log.subscribe(stream_name(self.id), from_position=cursor):
            yield event[0], await dereference_blob_refs(event[1])

    def __await__(self):
        return self.result().__await__()

    async def result(self, *, poll_interval: float = 0.05, turn_id: str | None = None) -> Any:
        while True:
            state = await self.state()
            if turn_id is not None:
                if turn_id in state.turn_results:
                    return state.turn_results[turn_id]
                if turn_id in state.turn_errors:
                    raise AgentSessionError(str(state.turn_errors[turn_id]))
            if state.status == "completed":
                return state.output
            if state.status == "failed":
                raise AgentSessionError(str(state.error or "Agent session failed"))
            if state.status == "cancelled":
                raise asyncio.CancelledError(f"Agent session {self.id} was cancelled")
            await asyncio.sleep(poll_interval)


async def session(session_id: str) -> Session:
    if await load_runstate(session_id) is None:
        raise KeyError(f"Unknown agent session {session_id!r}")
    return Session(session_id)
