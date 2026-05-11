"""RunState storage and outbox helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from skrift.agents.blob import offload_large_payload_fields
from skrift.agents.config import get_agents_config
from skrift.agents.models import (
    AGENT_EVENT_SCHEMA_VERSION,
    Actor,
    AgentRunJob,
    OutboxEvent,
    OutboxSubmit,
    OutboxWake,
    RunState,
)
from skrift.workers import get_runtime
from skrift.workers.models import utcnow

UpdateRunState = Callable[[RunState], RunState | Awaitable[RunState]]


def runstate_key(session_id: str) -> str:
    return f"runstate:{session_id}"


def stream_name(session_id: str) -> str:
    return f"agents:run:{session_id}"


async def load_runstate(session_id: str) -> RunState | None:
    value = await get_runtime().state_store.get(runstate_key(session_id))
    if value is None:
        return None
    if isinstance(value, RunState):
        return value
    return RunState.model_validate(value)


async def update_runstate(session_id: str, fn: UpdateRunState) -> RunState:
    should_snapshot = False

    async def wrapper(value: Any) -> RunState:
        nonlocal should_snapshot
        if value is None:
            raise KeyError(f"Unknown agent session {session_id!r}")
        state = value if isinstance(value, RunState) else RunState.model_validate(value)
        next_state = fn(state)
        if hasattr(next_state, "__await__"):
            next_state = await next_state  # type: ignore[assignment]
        next_state.version += 1
        next_state.last_active_at = utcnow()
        if _snapshot_due(next_state):
            next_state.last_snapshot_at = next_state.last_active_at
            should_snapshot = True
        return next_state

    updated = await get_runtime().state_store.update(runstate_key(session_id), wrapper)
    if should_snapshot:
        await _snapshot_runstate(updated)
    return updated


async def create_or_update_runstate(state: RunState) -> RunState:
    should_snapshot = False

    async def wrapper(value: Any) -> RunState:
        nonlocal should_snapshot
        if value is None:
            state.last_snapshot_at = utcnow()
            should_snapshot = True
            return state
        existing = value if isinstance(value, RunState) else RunState.model_validate(value)
        return existing

    updated = await get_runtime().state_store.update(runstate_key(state.session_id), wrapper)
    if should_snapshot:
        await _snapshot_runstate(updated)
    return updated


def next_event(state: RunState, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    state.last_seq += 1
    now = utcnow()
    return {
        "event_id": f"{state.session_id}:{state.last_seq}:{event_type}",
        "type": event_type,
        "session_id": state.session_id,
        "parent_session_id": state.parent_session_id,
        "seq": state.last_seq,
        "ts": now.isoformat(),
        "schema_version": AGENT_EVENT_SCHEMA_VERSION,
        "payload": payload,
    }


def append_event(state: RunState, event_type: str, payload: dict[str, Any]) -> None:
    state.outbox.append(
        OutboxEvent(
            stream=stream_name(state.session_id),
            event=next_event(state, event_type, payload),
        )
    )


def append_submit(state: RunState, job_id: str, *, queue: str | None = None) -> None:
    state.outbox.append(
        OutboxSubmit(
            job_type="agents.run",
            payload=AgentRunJob(
                session_id=state.session_id,
                agent_name=state.agent_name,
            ).model_dump(mode="json"),
            queue=queue or get_agents_config().default_queue,
            job_id=job_id,
        )
    )


def append_wake(state: RunState, job_id: str, resume_at: datetime | None = None) -> None:
    state.outbox.append(OutboxWake(job_id=job_id, resume_at=resume_at))


async def drain_outbox(session_id: str) -> None:
    runtime = get_runtime()
    while True:
        state = await load_runstate(session_id)
        if state is None or not state.outbox:
            return
        entry = state.outbox[0]
        if entry.kind == "event":
            await runtime.event_log.append(
                entry.stream,
                await offload_large_payload_fields(entry.event),
            )
        elif entry.kind == "submit":
            if await runtime.get_job_state(entry.job_id) is None:
                await runtime.submit(
                    entry.job_type,
                    entry.payload,
                    queue=entry.queue,
                    job_id=entry.job_id,
                )
        elif entry.kind == "wake":
            async def remove_wake(processed: RunState) -> RunState:
                processed.outbox = [
                    item for item in processed.outbox if item.entry_id != entry.entry_id
                ]
                return processed

            await update_runstate(session_id, remove_wake)
            await runtime.wake(entry.job_id, resume_at=entry.resume_at)
            continue
        else:
            raise ValueError(f"Unknown outbox entry kind {entry!r}")

        async def remove(processed: RunState) -> RunState:
            processed.outbox = [
                item for item in processed.outbox if item.entry_id != entry.entry_id
            ]
            return processed

        await update_runstate(session_id, remove)


async def drain_pending_outboxes() -> list[str]:
    """Drain all RunStates that currently have pending outbox entries."""

    runtime = get_runtime()
    drained: list[str] = []
    for key in await runtime.state_store.keys("runstate:"):
        state = await runtime.state_store.get(key)
        if state is None:
            continue
        runstate = state if isinstance(state, RunState) else RunState.model_validate(state)
        if not runstate.outbox:
            continue
        await drain_outbox(runstate.session_id)
        drained.append(runstate.session_id)
    return drained


def new_session_id() -> str:
    return uuid4().hex


def actor_payload(actor: Actor) -> dict[str, Any]:
    return actor.model_dump(mode="json")


def _snapshot_due(state: RunState) -> bool:
    if state.terminal_at is not None:
        return state.last_snapshot_at is None or state.last_snapshot_at < state.terminal_at
    interval = get_agents_config().state_snapshot_interval
    if interval <= 0:
        return False
    if state.last_snapshot_at is None:
        return True
    return (state.last_active_at - state.last_snapshot_at).total_seconds() >= interval


async def _snapshot_runstate(state: RunState) -> None:
    await get_runtime().archive.upsert_state_snapshot(runstate_key(state.session_id), state)
