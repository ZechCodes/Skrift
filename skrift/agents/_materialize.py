"""Materialization of Skrift agent facades into Pydantic AI agents.

This module is the single boundary that imports ``pydantic_ai``. It is only
imported when an agent actually runs (in a worker, or inline in the same
process), never by merely importing ``skrift`` or defining an agent. Keeping
the import here is what lets web/CMS processes dispatch agents to out-of-process
workers without paying the Pydantic AI memory and startup cost.
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Any, Callable

from pydantic_ai import Agent as PydanticAgent, DeferredToolRequests, RunContext
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred

from skrift.agents.approval import ApprovalContext, _record_tool_approval_decision
from skrift.agents.context import current_session_id
from skrift.agents.helpers import accepts_approval_context, callable_name
from skrift.workers.models import utcnow

if TYPE_CHECKING:
    from skrift.agents.agent import Agent, ToolSpec


class MaterializedAgent(PydanticAgent):
    """A live Pydantic AI agent built from a Skrift :class:`~skrift.agents.agent.Agent`.

    Holds the Pydantic AI run surface plus the wrappers that implement Skrift's
    approval gates and detached tools. It is created by :func:`materialize` and
    is never registered in the agent registry itself — the facade owns identity.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tool_policies: dict[str, Any] = {}

    def register_spec(self, spec: ToolSpec) -> None:
        func = spec.func
        if spec.approval_gate is not None:
            func = self._approval_gate_wrapper(
                func, spec.approval_gate, plain=spec.plain, detached=spec.detached
            )
        elif spec.detached:
            func = self._deferred_tool_wrapper(func)
        if spec.plain and spec.approval_gate is None:
            PydanticAgent.tool_plain(self, func, **spec.kwargs)
        else:
            PydanticAgent.tool(self, func, **spec.kwargs)

    def _approval_gate_wrapper(
        self,
        func: Callable[..., Any],
        gate: Callable[..., Any],
        *,
        plain: bool,
        detached: bool = False,
    ) -> Callable[..., Any]:
        call_func = self._deferred_tool_wrapper(func) if detached else func

        if plain:
            signature = inspect.signature(func)
            context_param = inspect.Parameter(
                "ctx",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=RunContext[Any],
            )

            @functools.wraps(func)
            async def plain_wrapper(ctx: RunContext[Any], *args: Any, **kwargs: Any) -> Any:
                await self._apply_dynamic_approval(ctx, gate, kwargs)
                result = call_func(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

            plain_wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
                parameters=[context_param, *signature.parameters.values()]
            )
            plain_wrapper.__annotations__ = {
                **getattr(func, "__annotations__", {}),
                "ctx": RunContext[Any],
            }
            return plain_wrapper

        @functools.wraps(func)
        async def context_wrapper(ctx: RunContext[Any], *args: Any, **kwargs: Any) -> Any:
            await self._apply_dynamic_approval(ctx, gate, kwargs)
            result = call_func(ctx, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        return context_wrapper

    async def _apply_dynamic_approval(
        self,
        ctx: RunContext[Any],
        gate: Callable[..., Any],
        args: dict[str, Any],
    ) -> None:
        if ctx.tool_call_approved:
            return
        gate_kwargs = dict(args)
        if accepts_approval_context(gate):
            gate_kwargs["ctx"] = ApprovalContext(
                session_id=current_session_id(),
                tool_call_id=ctx.tool_call_id,
                tool_name=ctx.tool_name,
                deps=ctx.deps,
                metadata=dict(ctx.metadata or {}),
            )
        gate_result = gate(**gate_kwargs)
        if inspect.isawaitable(gate_result):
            gate_result = await gate_result
        gated = bool(gate_result)
        decision = {
            "gated": gated,
            "policy": "callable",
            "callable_name": callable_name(gate),
        }
        await _record_tool_approval_decision(ctx, args, decision)
        if gated:
            raise ApprovalRequired({"skrift_approval_decision": decision})

    @staticmethod
    def _deferred_tool_wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            raise CallDeferred({"skrift_detached": True})

        return wrapper

    async def _run_pydantic(self, *args: Any, **kwargs: Any) -> Any:
        return await super().run(*args, **kwargs)

    def _iter_pydantic(self, *args: Any, **kwargs: Any) -> Any:
        return super().iter(*args, **kwargs)

    def definition_snapshot(self) -> dict[str, Any]:
        return {
            "model_id": str(getattr(self, "model", "")),
            "system_prompt": "\n\n".join(str(prompt) for prompt in getattr(self, "_system_prompts", ())),
            "system_prompts": [str(prompt) for prompt in getattr(self, "_system_prompts", ())],
            "instructions": _snapshot_callables(getattr(self, "_instructions", None)),
            "system_prompt_functions": _snapshot_callables(
                getattr(self, "_system_prompt_functions", ())
            ),
            "dynamic_system_prompt_functions": _snapshot_callables(
                getattr(self, "_system_prompt_dynamic_functions", {})
            ),
            "output_type": _safe_name(getattr(self, "_output_type", None)),
            "output_type_schema": _output_schema_snapshot(getattr(self, "_output_schema", None)),
            "tools": [
                {"name": name, "policy": policy.model_dump(mode="json")}
                for name, policy in sorted(self._tool_policies.items())
            ],
            "snapshot_at": utcnow().isoformat(),
        }


def materialize(facade: Agent) -> MaterializedAgent:
    """Build a live Pydantic AI agent from a Skrift agent facade."""

    kwargs = dict(facade._init_kwargs)
    kwargs["output_type"] = durable_registration_output_type(
        facade._init_kwargs.get("output_type", str)
    )
    agent = MaterializedAgent(*facade._init_args, name=facade.skrift_name, **kwargs)
    agent._tool_policies = facade._tool_policies
    for callback in facade._callback_specs:
        register = getattr(PydanticAgent, callback.kind)
        if callback.kind == "output_validator":
            # output_validator is a bare decorator: func is required, no kwargs.
            register(agent, callback.func)
        else:
            # Use the decorator-factory form (kwargs first, then the function):
            # the bare `system_prompt(func, dynamic=True)` form is rejected.
            register(agent, **callback.kwargs)(callback.func)
    for spec in facade._tool_specs:
        agent.register_spec(spec)
    return agent


def durable_registration_output_type(output_type: Any) -> Any:
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


def _safe_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return repr(value)


def _snapshot_callables(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        items = value.values()
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = (value,)
    return [_safe_name(item) or "" for item in items]


def _output_schema_snapshot(schema: Any) -> dict[str, Any]:
    if schema is None:
        return {}
    text_processor = getattr(schema, "text_processor", None)
    object_def = getattr(text_processor, "object_def", None)
    toolset = getattr(schema, "toolset", None)
    tools = []
    for definition in getattr(toolset, "_tool_defs", ()) or ():
        tools.append(
            {
                "name": getattr(definition, "name", None),
                "description": getattr(definition, "description", None),
                "parameters_json_schema": getattr(definition, "parameters_json_schema", None),
                "kind": getattr(definition, "kind", None),
            }
        )
    return {
        "schema_kind": type(schema).__name__,
        "allows_none": getattr(schema, "allows_none", None),
        "allows_deferred_tools": getattr(schema, "allows_deferred_tools", None),
        "allows_image": getattr(schema, "allows_image", None),
        "object": {
            "name": getattr(object_def, "name", None),
            "description": getattr(object_def, "description", None),
            "strict": getattr(object_def, "strict", None),
            "json_schema": getattr(object_def, "json_schema", None),
        }
        if object_def is not None
        else None,
        "tools": tools,
    }
