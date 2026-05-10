"""Public artifact helpers for durable agent runs."""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext
from pydantic_core import PydanticSerializationError, to_jsonable_python

from skrift.agents.context import current_session_id
from skrift.agents.state import append_event, update_runstate


async def record_artifact(ctx: RunContext[Any], value: Any, *, kind: str) -> None:
    """Record a durable artifact created during the current agent run."""

    if not isinstance(kind, str) or not kind:
        raise ValueError("Artifact kind is required")
    session_id = current_session_id()
    if session_id is None:
        raise RuntimeError("No active Skrift agent session")
    try:
        json_value = to_jsonable_python(value)
    except PydanticSerializationError as exc:
        raise ValueError("Artifact value must be JSON-serializable") from exc

    async def mutate(state):
        append_event(
            state,
            "ToolArtifact",
            {
                "kind": kind,
                "value": json_value,
                "tool_call_id": ctx.tool_call_id,
                "tool_name": ctx.tool_name,
            },
        )
        return state

    await update_runstate(session_id, mutate)


async def attach_artifact(ctx: RunContext[Any], value: Any, *, kind: str) -> None:
    """Alias for :func:`record_artifact`."""

    await record_artifact(ctx, value, kind=kind)
