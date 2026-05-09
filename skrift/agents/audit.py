"""Replay and audit export helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from skrift.agents.blob import dereference_blob_refs
from skrift.agents.state import load_runstate, stream_name
from skrift.workers import get_runtime
from skrift.workers.models import utcnow


class AuditTrail(BaseModel):
    session_id: str
    agent_name: str
    started_at: str | None = None
    terminal_at: str | None = None
    terminal_status: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    actors: list[dict[str, Any]] = Field(default_factory=list)
    tools_called: dict[str, int] = Field(default_factory=dict)
    export_metadata: dict[str, Any] = Field(default_factory=dict)


async def replay(session_id: str, until: int | None = None) -> list[dict[str, Any]]:
    runtime = get_runtime()
    stream = stream_name(session_id)
    rows = await runtime.event_log.read(stream, limit=until)
    if not rows:
        rows = await runtime.archive.query_events(stream)
        if until is not None:
            rows = rows[:until]
    return [await dereference_blob_refs(event) for _, event in rows]


async def audit_export(
    session_id: str,
    *,
    include_lineage: bool = True,
    format: str = "flat",
) -> AuditTrail:
    state = await load_runstate(session_id)
    if state is None:
        raise KeyError(f"Unknown agent session {session_id!r}")
    included_session_ids = [session_id]
    if include_lineage:
        included_session_ids = await _lineage_session_ids(session_id)
    events: list[dict[str, Any]] = []
    for included_session_id in included_session_ids:
        for event in await replay(included_session_id):
            events.append({"session_stream_id": included_session_id, **event})
    events.sort(key=lambda event: (str(event.get("ts", "")), int(event.get("seq", 0))))
    actors: dict[str, dict[str, Any]] = {}
    tools: dict[str, int] = {}
    for event in events:
        payload = event.get("payload", {})
        actor = payload.get("actor")
        if isinstance(actor, dict):
            actors[f"{actor.get('kind')}:{actor.get('id')}"] = actor
        tool_name = payload.get("tool_name")
        if tool_name:
            tools[str(tool_name)] = tools.get(str(tool_name), 0) + 1
    return AuditTrail(
        session_id=session_id,
        agent_name=state.agent_name,
        started_at=state.started_at.isoformat() if state.started_at else None,
        terminal_at=state.terminal_at.isoformat() if state.terminal_at else None,
        terminal_status=state.status if state.terminal_at else None,
        lineage={
            "parent_session_id": state.parent_session_id,
            "root_session_id": state.root_session_id,
            "included_session_ids": included_session_ids,
            "include_lineage": include_lineage,
            "format": format,
        },
        events=events,
        actors=list(actors.values()),
        tools_called=tools,
        export_metadata={
            "exported_at": utcnow().isoformat(),
            "exporter_version": "1",
            "retention_status": "hot_event_log",
        },
    )


async def _lineage_session_ids(session_id: str) -> list[str]:
    runtime = get_runtime()
    root_state = await load_runstate(session_id)
    if root_state is None:
        return [session_id]
    root_id = root_state.root_session_id or root_state.session_id
    session_ids: set[str] = {session_id}
    for key in await runtime.state_store.keys("runstate:"):
        value = await runtime.state_store.get(key)
        if value is None:
            continue
        state = value if hasattr(value, "session_id") else None
        if state is None:
            from skrift.agents.models import RunState

            state = RunState.model_validate(value)
        if state.session_id == root_id or state.root_session_id == root_id:
            session_ids.add(state.session_id)
    return sorted(session_ids)
