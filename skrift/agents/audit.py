"""Replay and audit export helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from skrift.agents.blob import dereference_blob_refs
from skrift.agents.models import AgentUsageRecord, AgentUsageTotals
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
    usage_records: list[AgentUsageRecord] = Field(default_factory=list)
    usage_totals: AgentUsageTotals = Field(default_factory=AgentUsageTotals)
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
    usage_records: dict[str, AgentUsageRecord] = {}
    for event in events:
        payload = event.get("payload", {})
        actor = payload.get("actor")
        if isinstance(actor, dict):
            actors[f"{actor.get('kind')}:{actor.get('id')}"] = actor
        tool_name = payload.get("tool_name")
        if tool_name:
            tools[str(tool_name)] = tools.get(str(tool_name), 0) + 1
        if event.get("type") == "AgentUsageRecorded":
            try:
                record = AgentUsageRecord.model_validate(payload)
            except Exception:
                continue
            usage_records[f"{record.session_id}:{record.turn_id}"] = record
    if not usage_records:
        for record in state.turn_usage.values():
            usage_records[f"{record.session_id}:{record.turn_id}"] = record
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
        usage_records=sorted(
            usage_records.values(),
            key=lambda record: (record.recorded_at, record.session_id, record.turn_id),
        ),
        usage_totals=_sum_usage_records(usage_records.values()),
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


def _sum_usage_records(records: Any) -> AgentUsageTotals:
    totals = AgentUsageTotals()
    for record in records:
        details = dict(totals.details)
        for key, value in record.details.items():
            details[key] = details.get(key, 0) + value
        totals = AgentUsageTotals(
            requests=totals.requests + record.requests,
            tool_calls=totals.tool_calls + record.tool_calls,
            input_tokens=totals.input_tokens + record.input_tokens,
            cache_write_tokens=totals.cache_write_tokens + record.cache_write_tokens,
            cache_read_tokens=totals.cache_read_tokens + record.cache_read_tokens,
            output_tokens=totals.output_tokens + record.output_tokens,
            input_audio_tokens=totals.input_audio_tokens + record.input_audio_tokens,
            cache_audio_read_tokens=totals.cache_audio_read_tokens + record.cache_audio_read_tokens,
            output_audio_tokens=totals.output_audio_tokens + record.output_audio_tokens,
            details=details,
        )
    return totals
