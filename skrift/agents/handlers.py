"""Pydantic AI-free registration of agent worker handlers.

The site process must be able to *enqueue* agent jobs (which requires the
handler descriptors — job type, payload model, queue — to be registered) without
importing :mod:`skrift.agents.runtime`, since that module imports ``pydantic_ai``.

Each handler is registered with a thin proxy that imports the real runtime
handler only when a job actually executes — i.e. in the worker (or inline in the
same process). Submitting a job never calls the proxy, so pure dispatch stays
free of ``pydantic_ai``.
"""

from __future__ import annotations

from typing import Any

from skrift.agents.models import AgentRunJob, AgentToolCallJob
from skrift.workers.registry import registry as worker_registry


async def _run_proxy(payload: AgentRunJob, context: Any) -> Any:
    from skrift.agents.runtime import agents_run_handler

    return await agents_run_handler(payload, context)


async def _run_dead_proxy(entry: Any) -> Any:
    from skrift.agents.runtime import agents_run_dead

    return await agents_run_dead(entry)


async def _tool_call_proxy(payload: AgentToolCallJob, context: Any) -> Any:
    from skrift.agents.runtime import agents_tool_call_handler

    return await agents_tool_call_handler(payload, context)


async def _tool_call_dead_proxy(entry: Any) -> Any:
    from skrift.agents.runtime import agents_tool_call_dead

    return await agents_tool_call_dead(entry)


def register_agent_handlers() -> None:
    """Register agent worker handlers, idempotently and without ``pydantic_ai``.

    Safe to call from the site (to enable dispatch) and from the worker (to make
    handlers executable). Re-registers after tests or hosts clear the registry.
    """

    try:
        worker_registry.get("agents.run")
    except KeyError:
        worker_registry.register(
            "agents.run",
            _run_proxy,
            payload_model=AgentRunJob,
            queue="agents",
        )
        worker_registry.set_dead_callback("agents.run", _run_dead_proxy)
    try:
        worker_registry.get("agents.tool_call")
    except KeyError:
        worker_registry.register(
            "agents.tool_call",
            _tool_call_proxy,
            payload_model=AgentToolCallJob,
            queue="agents",
        )
        worker_registry.set_dead_callback("agents.tool_call", _tool_call_dead_proxy)
