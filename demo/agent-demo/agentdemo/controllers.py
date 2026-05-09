"""Realtime browser controller for the Skrift agent demo."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import skrift
from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse

from skrift.agents.state import load_runstate
from skrift.lib.hooks import APP_SHUTDOWN, action
from skrift.lib.notifications import NotificationMode, _ensure_nid, notify_session

from agentdemo.agents import AGENT_NAME, assistant


logger = logging.getLogger(__name__)
_watch_tasks: set[asyncio.Task] = set()


def _gemini_configured() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def _remember_task(task: asyncio.Task) -> None:
    _watch_tasks.add(task)
    task.add_done_callback(_watch_tasks.discard)


async def _payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _event_message(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = str(event.get("type", "AgentEvent"))
    if event_type == "UserMessageReceived":
        return "User message queued." if payload.get("queued") else "User message received."
    if event_type == "UserMessageActivated":
        return "Queued message is now running."
    if event_type == "AssistantMessageCompleted":
        return str(payload.get("message", ""))
    if event_type == "AgentCompleted":
        return "Agent run completed."
    if event_type == "AgentFailed":
        return str(payload.get("exception_message") or "Agent run failed.")
    if event_type == "ToolCallAwaitingApproval":
        return f"Tool awaiting approval: {payload.get('tool_name', 'unknown')}"
    if event_type == "ToolCallRejected":
        return "Pending tool approval was cancelled."
    if event_type == "ToolCallDispatched":
        return f"Tool dispatched: {payload.get('tool_name', 'unknown')}"
    if event_type == "ToolCallCompleted":
        return f"Tool completed: {payload.get('tool_call_id', 'unknown')}"
    return event_type


async def _notify_agent_event(nid: str, session_id: str, event: dict[str, Any]) -> None:
    event_type = str(event.get("type", "AgentEvent"))
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    notification_type = "agent.event"
    if event_type == "AssistantMessageCompleted":
        notification_type = "agent.message"
    elif event_type in {"AgentCompleted", "AgentFailed", "AgentCancelled"}:
        notification_type = "agent.status"
    elif event_type == "UserMessageActivated":
        notification_type = "agent.turn.active"

    state = await load_runstate(session_id)
    await notify_session(
        nid,
        notification_type,
        mode=NotificationMode.TIMESERIES,
        session_id=session_id,
        agent_name=AGENT_NAME,
        event_type=event_type,
        seq=event.get("seq"),
        status=state.status if state else event_type,
        pending_turns=len(state.pending_user_messages) if state else 0,
        active_turn=bool(state and state.current_run_job_id),
        message=_event_message(event),
        payload=payload,
    )


async def _watcher_should_stop(session_id: str, event_type: str, seq: int) -> bool:
    if event_type not in {"AgentCompleted", "AgentFailed", "AgentCancelled"}:
        return False
    state = await load_runstate(session_id)
    if state is None:
        return True
    return (
        seq >= state.last_seq
        and state.status in {"completed", "failed", "cancelled"}
        and not state.pending_user_messages
        and state.current_run_job_id is None
    )


async def _watch_agent_events(nid: str, session_id: str, *, from_seq: int = 0) -> None:
    try:
        session = await skrift.session(session_id)
        async for _, event in session:
            seq = int(event.get("seq") or 0)
            if seq <= from_seq:
                continue
            await _notify_agent_event(nid, session_id, event)
            if await _watcher_should_stop(session_id, str(event.get("type", "")), seq):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Agent demo watcher failed for session %s", session_id)
        await notify_session(
            nid,
            "agent.error",
            mode=NotificationMode.TIMESERIES,
            session_id=session_id,
            message="Agent event watcher failed; check server logs.",
        )


async def _start_watcher(nid: str, session_id: str, *, from_seq: int = 0) -> None:
    task = asyncio.create_task(
        _watch_agent_events(nid, session_id, from_seq=from_seq),
        name=f"agent-demo-watch-{session_id}",
    )
    _remember_task(task)


@action(APP_SHUTDOWN)
async def stop_agent_demo_watchers(_app) -> None:
    for task in list(_watch_tasks):
        task.cancel()
    if _watch_tasks:
        await asyncio.gather(*_watch_tasks, return_exceptions=True)
    _watch_tasks.clear()


class AgentDemoController(Controller):
    """Chat-style demo for durable Skrift agents."""

    path = "/"

    @get("/")
    async def index(self, request: Request) -> TemplateResponse:
        return TemplateResponse(
            "agent-demo/index.html",
            context={
                "nid": _ensure_nid(request),
                "model_name": os.getenv("AGENT_DEMO_MODEL", "gemini-3.1-flash-lite-preview"),
                "gemini_configured": _gemini_configured(),
            },
        )

    @get("/audit")
    async def audit(self, request: Request) -> TemplateResponse:
        session_id = str(request.query_params.get("session_id") or "").strip()
        audit_payload: dict[str, Any] | None = None
        audit_error = ""
        if session_id:
            try:
                audit_payload = (
                    await skrift.audit_export(session_id, include_lineage=True)
                ).model_dump(mode="json")
            except KeyError:
                audit_error = f"Unknown session {session_id!r}."

        return TemplateResponse(
            "agent-demo/audit.html",
            context={
                "session_id": session_id,
                "audit": audit_payload,
                "audit_json": json.dumps(audit_payload, indent=2, sort_keys=True)
                if audit_payload
                else "",
                "audit_error": audit_error,
            },
        )

    @post("/agent/sessions")
    async def start_session(self, request: Request) -> dict[str, Any]:
        if not _gemini_configured():
            return {
                "ok": False,
                "error": "GEMINI_API_KEY is not configured for this process.",
            }
        data = await _payload(request)
        message = str(data.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "Message is required."}

        nid = _ensure_nid(request)
        session = await assistant.run(message, actor={"kind": "user", "id": f"session:{nid}"})
        await notify_session(
            nid,
            "agent.session.started",
            mode=NotificationMode.TIMESERIES,
            session_id=session.id,
            agent_name=AGENT_NAME,
            message="Agent session queued.",
            status="queued",
        )
        await _start_watcher(nid, session.id)
        state = await session.state()
        return {
            "ok": True,
            "session_id": session.id,
            "status": state.status,
            "pending_turns": len(state.pending_user_messages),
            "active_turn": bool(state.current_run_job_id),
        }

    @post("/agent/sessions/{session_id:str}/messages")
    async def send_message(self, request: Request, session_id: str) -> dict[str, Any]:
        if not _gemini_configured():
            return {
                "ok": False,
                "error": "GEMINI_API_KEY is not configured for this process.",
            }
        data = await _payload(request)
        message = str(data.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "Message is required."}

        nid = _ensure_nid(request)
        state = await load_runstate(session_id)
        if state is None:
            return {"ok": False, "error": f"Unknown session {session_id!r}."}
        from_seq = state.last_seq
        session = await skrift.session(session_id)
        await session.send(message, actor={"kind": "user", "id": f"session:{nid}"})
        await notify_session(
            nid,
            "agent.session.turn_queued",
            mode=NotificationMode.TIMESERIES,
            session_id=session_id,
            agent_name=AGENT_NAME,
            message="Follow-up turn queued.",
            status="queued",
        )
        await _start_watcher(nid, session_id, from_seq=from_seq)
        updated = await session.state()
        return {
            "ok": True,
            "session_id": session_id,
            "status": updated.status,
            "pending_turns": len(updated.pending_user_messages),
            "active_turn": bool(updated.current_run_job_id),
        }

    @get("/agent/sessions/{session_id:str}")
    async def inspect_session(self, session_id: str) -> dict[str, Any]:
        state = await load_runstate(session_id)
        if state is None:
            return {"ok": False, "error": f"Unknown session {session_id!r}."}
        return {
            "ok": True,
            "session_id": state.session_id,
            "status": state.status,
            "messages": state.messages,
            "pending_user_messages": state.pending_user_messages,
            "pending_turns": len(state.pending_user_messages),
            "active_turn": bool(state.current_run_job_id),
            "last_seq": state.last_seq,
            "output": state.output,
            "error": state.error,
        }
