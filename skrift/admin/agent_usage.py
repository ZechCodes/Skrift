"""Agent usage admin dashboard."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.agents.models import AgentUsageRecord, AgentUsageTotals, RunState
from skrift.auth.guards import Permission, auth_guard
from skrift.lib.flash import get_flash_messages
from skrift.workers import get_runtime


AGENT_USAGE_RUN_LIMIT = 100
AGENT_USAGE_GROUP_LIMIT = 50


class AgentUsageAdminController(Controller):
    """Read-only dashboard for durable agent LLM usage."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/agent-usage",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Agent Usage", "icon": "bar-chart-3", "order": 94},
    )
    async def agent_usage(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show aggregated LLM token usage from durable agent sessions."""
        ctx = await get_admin_context(request, db_session)
        dashboard = await build_agent_usage_dashboard(get_runtime())
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/agent_usage.html",
            context={
                "flash_messages": flash_messages,
                "dashboard": dashboard,
                **ctx,
            },
        )


async def build_agent_usage_dashboard(runtime: Any) -> dict[str, Any]:
    states = await _load_runstates(runtime)
    records_by_run: dict[str, list[AgentUsageRecord]] = {}
    state_by_run: dict[str, RunState] = {}
    for state in states:
        if not state.turn_usage:
            continue
        state_by_run[state.session_id] = state
        records_by_run[state.session_id] = list(state.turn_usage.values())

    run_rows = [
        _run_row(state_by_run[session_id], records)
        for session_id, records in records_by_run.items()
    ]
    run_rows.sort(key=lambda row: row["sort_at"], reverse=True)

    return {
        "overall": _summary_row("Overall", _sum_records(_all_records(records_by_run.values()))),
        "runs": run_rows[:AGENT_USAGE_RUN_LIMIT],
        "agents": _group_rows(
            _all_records(records_by_run.values()),
            lambda record: record.agent_name or "unknown",
        ),
        "actors": _group_rows(
            _all_records(records_by_run.values()),
            lambda record: _actor_key(record),
        ),
        "models": _group_rows(
            _all_records(records_by_run.values()),
            lambda record: record.model_name or record.configured_model or "unknown",
        ),
        "run_count": len(run_rows),
        "turn_count": sum(len(records) for records in records_by_run.values()),
    }


async def _load_runstates(runtime: Any) -> list[RunState]:
    states: list[RunState] = []
    for key in await runtime.state_store.keys("runstate:"):
        value = await runtime.state_store.get(key)
        if value is None:
            continue
        if isinstance(value, RunState):
            states.append(value)
        else:
            states.append(RunState.model_validate(value))
    return states


def _run_row(state: RunState, records: list[AgentUsageRecord]) -> dict[str, Any]:
    totals = _sum_records(records)
    models = sorted(
        {
            record.model_name or record.configured_model or "unknown"
            for record in records
        }
    )
    latest = max((record.recorded_at for record in records), default=state.last_active_at)
    return {
        **_summary_row(state.session_id, totals),
        "session_id": state.session_id,
        "short_session_id": state.session_id[:12],
        "agent_name": state.agent_name,
        "actor": _actor_key(records[-1]) if records else _actor_from_state(state),
        "models": ", ".join(models),
        "turns": len(records),
        "status": state.status,
        "last_recorded_at": latest.strftime("%Y-%m-%d %H:%M:%S"),
        "sort_at": latest,
    }


def _group_rows(
    records: Iterable[AgentUsageRecord],
    key_fn: Callable[[AgentUsageRecord], str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[AgentUsageRecord]] = {}
    for record in records:
        grouped.setdefault(key_fn(record), []).append(record)
    rows = [
        {
            **_summary_row(key, _sum_records(group_records)),
            "runs": len({record.session_id for record in group_records}),
            "turns": len(group_records),
        }
        for key, group_records in grouped.items()
    ]
    rows.sort(key=lambda row: row["total_tokens"], reverse=True)
    return rows[:AGENT_USAGE_GROUP_LIMIT]


def _summary_row(label: str, totals: AgentUsageTotals) -> dict[str, Any]:
    total_tokens = (
        totals.input_tokens
        + totals.cache_write_tokens
        + totals.cache_read_tokens
        + totals.output_tokens
        + totals.input_audio_tokens
        + totals.cache_audio_read_tokens
        + totals.output_audio_tokens
    )
    return {
        "label": label,
        "requests": totals.requests,
        "tool_calls": totals.tool_calls,
        "input_tokens": totals.input_tokens,
        "cache_write_tokens": totals.cache_write_tokens,
        "cache_read_tokens": totals.cache_read_tokens,
        "output_tokens": totals.output_tokens,
        "input_audio_tokens": totals.input_audio_tokens,
        "cache_audio_read_tokens": totals.cache_audio_read_tokens,
        "output_audio_tokens": totals.output_audio_tokens,
        "total_tokens": total_tokens,
        "requests_display": _fmt(totals.requests),
        "tool_calls_display": _fmt(totals.tool_calls),
        "input_tokens_display": _fmt(totals.input_tokens),
        "cache_write_tokens_display": _fmt(totals.cache_write_tokens),
        "cache_read_tokens_display": _fmt(totals.cache_read_tokens),
        "output_tokens_display": _fmt(totals.output_tokens),
        "total_tokens_display": _fmt(total_tokens),
    }


def _sum_records(records: Iterable[AgentUsageRecord]) -> AgentUsageTotals:
    total = AgentUsageTotals()
    for record in records:
        details = dict(total.details)
        for key, value in record.details.items():
            details[key] = details.get(key, 0) + value
        total = AgentUsageTotals(
            requests=total.requests + record.requests,
            tool_calls=total.tool_calls + record.tool_calls,
            input_tokens=total.input_tokens + record.input_tokens,
            cache_write_tokens=total.cache_write_tokens + record.cache_write_tokens,
            cache_read_tokens=total.cache_read_tokens + record.cache_read_tokens,
            output_tokens=total.output_tokens + record.output_tokens,
            input_audio_tokens=total.input_audio_tokens + record.input_audio_tokens,
            cache_audio_read_tokens=total.cache_audio_read_tokens + record.cache_audio_read_tokens,
            output_audio_tokens=total.output_audio_tokens + record.output_audio_tokens,
            details=details,
        )
    return total


def _all_records(groups: Iterable[Iterable[AgentUsageRecord]]) -> list[AgentUsageRecord]:
    return [record for group in groups for record in group]


def _actor_key(record: AgentUsageRecord) -> str:
    return f"{record.actor.kind}:{record.actor.id}"


def _actor_from_state(state: RunState) -> str:
    return f"{state.created_by.kind}:{state.created_by.id}"


def _fmt(value: int) -> str:
    return f"{value:,}"
