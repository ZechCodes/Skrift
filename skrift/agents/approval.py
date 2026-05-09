"""Approval helpers for Skrift agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_core import PydanticSerializationError, to_jsonable_python

from skrift.agents.context import current_session_id
from skrift.agents.state import append_event, update_runstate


@dataclass(frozen=True)
class ApprovalContext:
    """Narrow context passed to callable approval gates."""

    session_id: str | None
    tool_call_id: str | None
    tool_name: str | None
    deps: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


async def require_approval(
    ctx: RunContext[Any],
    *,
    reason: str,
    payload: Any | None = None,
) -> None:
    """Pause a context tool until the current tool call is approved."""

    if ctx.tool_call_approved:
        return
    json_payload = _jsonable_payload(payload)
    decision = {
        "gated": True,
        "policy": "runtime",
        "reason": reason,
    }
    metadata = {
        "skrift_runtime_approval": {
            "reason": reason,
            "payload": json_payload,
        },
        "skrift_approval_decision": decision,
    }
    await _record_tool_approval_decision(ctx, {}, decision)
    raise ApprovalRequired(metadata)


async def _record_tool_approval_decision(
    ctx: RunContext[Any],
    args: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    session_id = current_session_id()
    if not session_id:
        return

    async def mutate(state):
        append_event(
            state,
            "ToolApprovalDecision",
            {
                "tool_call_id": ctx.tool_call_id,
                "tool_name": ctx.tool_name,
                "args": args,
                "approval_decision": decision,
            },
        )
        return state

    await update_runstate(session_id, mutate)


def _jsonable_payload(payload: Any | None) -> Any | None:
    if payload is None:
        return None
    try:
        return to_jsonable_python(payload)
    except PydanticSerializationError as exc:
        raise ValueError("Approval payload must be JSON-serializable") from exc
