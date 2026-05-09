"""Skrift Agent wrapper around Pydantic AI."""

from __future__ import annotations

import functools
from typing import Any, Callable
from uuid import uuid4

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.exceptions import CallDeferred

from skrift.agents.config import get_agents_config
from skrift.agents.context import current_session_id, resolve_actor
from skrift.agents.models import ResumeContext, RunState, ToolPolicy
from skrift.agents.registry import AgentDefinition, registry
from skrift.agents.session import AgentSessionError, Session
from skrift.agents.state import (
    actor_payload,
    append_event,
    append_submit,
    create_or_update_runstate,
    drain_outbox,
    load_runstate,
    new_session_id,
    update_runstate,
)
from skrift.agents.turns import normalize_turn_kwargs
from skrift.workers.models import utcnow


class Agent(PydanticAgent):
    """Durable Skrift agent.

    The public `run` method queues a worker-backed run and returns a `Session`.
    The worker calls `_run_pydantic` to execute the underlying Pydantic AI agent.
    """

    def __init__(
        self,
        *args: Any,
        name: str,
        deps_factory: Callable[[ResumeContext], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        deps_type = kwargs.get("deps_type")
        if deps_type not in (None, type(None)) and deps_factory is None:
            raise TypeError("Skrift Agent requires deps_factory when deps_type is set")
        super().__init__(*args, name=name, **kwargs)
        self.skrift_name = name
        self.deps_factory = deps_factory
        self._tool_policies: dict[str, ToolPolicy] = {}
        self._detached_tools: dict[str, Callable[..., Any]] = {}
        registry.register(
            AgentDefinition(
                name=name,
                agent=self,
                deps_factory=deps_factory,
                tool_policies=self._tool_policies,
            )
        )

    def tool(
        self,
        func: Any = None,
        /,
        *,
        approval: bool | Callable[..., bool] = False,
        idempotent: bool = False,
        detached: bool = False,
        approval_on_retry: bool = False,
        policy_description: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if detached:
            raise NotImplementedError(
                "detached=True is not yet supported for context tools (@agent.tool). "
                "It works for @agent.tool_plain. If your tool needs deps, either "
                "restructure it as a plain tool that takes identifying args and "
                "looks up resources internally, or wait for the context rehydration path."
            )
        metadata = dict(kwargs.pop("metadata", {}) or {})
        if approval and "requires_approval" not in kwargs:
            kwargs["requires_approval"] = True
        metadata["skrift_policy"] = ToolPolicy(
            approval=bool(approval),
            idempotent=idempotent,
            detached=detached,
            approval_on_retry=approval_on_retry,
            policy_description=policy_description,
        ).model_dump(mode="json")
        original_func = func
        if detached and func is not None:
            func = self._deferred_tool_wrapper(func)
        decorator = super().tool(func, metadata=metadata, **kwargs)
        if func is not None:
            self._record_tool_policy(
                kwargs.get("name") or getattr(original_func, "__name__", ""),
                metadata["skrift_policy"],
            )
            if detached and original_func is not None:
                self._record_detached_tool(
                    kwargs.get("name") or getattr(original_func, "__name__", ""),
                    original_func,
                )
            return decorator
        return self._wrap_tool_decorator(
            decorator,
            kwargs.get("name"),
            metadata["skrift_policy"],
            detached=detached,
        )

    def tool_plain(
        self,
        func: Any = None,
        /,
        *,
        approval: bool | Callable[..., bool] = False,
        idempotent: bool = False,
        detached: bool = False,
        approval_on_retry: bool = False,
        policy_description: str | None = None,
        **kwargs: Any,
    ) -> Any:
        metadata = dict(kwargs.pop("metadata", {}) or {})
        if approval and "requires_approval" not in kwargs:
            kwargs["requires_approval"] = True
        metadata["skrift_policy"] = ToolPolicy(
            approval=bool(approval),
            idempotent=idempotent,
            detached=detached,
            approval_on_retry=approval_on_retry,
            policy_description=policy_description,
        ).model_dump(mode="json")
        original_func = func
        if detached and func is not None:
            func = self._deferred_tool_wrapper(func)
        decorator = super().tool_plain(func, metadata=metadata, **kwargs)
        if func is not None:
            self._record_tool_policy(
                kwargs.get("name") or getattr(original_func, "__name__", ""),
                metadata["skrift_policy"],
            )
            if detached and original_func is not None:
                self._record_detached_tool(
                    kwargs.get("name") or getattr(original_func, "__name__", ""),
                    original_func,
                )
            return decorator
        return self._wrap_tool_decorator(
            decorator,
            kwargs.get("name"),
            metadata["skrift_policy"],
            detached=detached,
        )

    def _wrap_tool_decorator(
        self,
        decorator: Any,
        explicit_name: str | None,
        policy: dict[str, Any],
        *,
        detached: bool = False,
    ) -> Any:
        if not callable(decorator):
            return decorator

        def wrapped(func: Any) -> Any:
            self._record_tool_policy(explicit_name or getattr(func, "__name__", ""), policy)
            if detached:
                self._record_detached_tool(explicit_name or getattr(func, "__name__", ""), func)
                return decorator(self._deferred_tool_wrapper(func))
            return decorator(func)

        return wrapped

    def _record_tool_policy(self, name: str, policy: dict[str, Any]) -> None:
        if name:
            self._tool_policies[name] = ToolPolicy.model_validate(policy)

    def _record_detached_tool(self, name: str, func: Callable[..., Any]) -> None:
        if name:
            self._detached_tools[name] = func

    @staticmethod
    def _deferred_tool_wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            raise CallDeferred({"skrift_detached": True})

        return wrapper

    async def run(
        self,
        user_prompt: Any = None,
        *,
        dispatch: str | None = None,
        session_id: str | None = None,
        actor: Any = None,
        deps_ref: dict[str, Any] | None = None,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        **kwargs: Any,
    ) -> Session:
        dispatch = dispatch or get_agents_config().default_subagent_dispatch
        if dispatch not in {"queued", "same_worker"}:
            raise ValueError("dispatch must be 'queued' or 'same_worker'")
        from skrift.agents.runtime import register_agent_handlers

        register_agent_handlers()
        resolved = resolve_actor(actor)
        sid = session_id or new_session_id()
        job_id = uuid4().hex
        turn_id = uuid4().hex
        run_kwargs = normalize_turn_kwargs(kwargs)
        inherited_parent_session_id = parent_session_id or current_session_id()
        inherited_root_session_id = root_session_id
        if inherited_parent_session_id and inherited_root_session_id is None:
            parent_state = await load_runstate(inherited_parent_session_id)
            inherited_root_session_id = (
                parent_state.root_session_id if parent_state else inherited_parent_session_id
            )
        if session_id is not None and await load_runstate(sid) is not None:
            raise AgentSessionError(f"Agent session {sid!r} already exists")
        state = RunState(
            session_id=sid,
            agent_name=self.skrift_name,
            status="queued",
            current_run_job_id=job_id,
            current_turn_id=turn_id if user_prompt is not None else None,
            messages=[{"role": "user", "content": user_prompt, "turn_id": turn_id}]
            if user_prompt is not None
            else [],
            deps_ref=deps_ref or {},
            parent_session_id=inherited_parent_session_id,
            root_session_id=inherited_root_session_id or inherited_parent_session_id or sid,
            run_kwargs=run_kwargs,
            created_by=resolved,
        )
        append_event(
            state,
            "UserMessageReceived",
            {
                "message": user_prompt,
                "actor": actor_payload(resolved),
                "turn_id": turn_id,
                "turn_index": 0,
                "queued": False,
                "turn_config": run_kwargs,
            },
        )
        append_submit(state, job_id)
        await create_or_update_runstate(state)
        await drain_outbox(sid)
        if inherited_parent_session_id:
            async def emit_dispatch(parent_state: RunState) -> RunState:
                append_event(
                    parent_state,
                    "SubAgentDispatched",
                    {
                        "child_session_id": sid,
                        "child_agent_name": self.skrift_name,
                        "dispatch_kind": dispatch,
                        "parent_tool_call_id": None,
                    },
                )
                return parent_state

            await update_runstate(inherited_parent_session_id, emit_dispatch)
            await drain_outbox(inherited_parent_session_id)
        return Session(sid)

    def chat(
        self,
        key: str,
        *,
        actor: Any = None,
        deps_ref: dict[str, Any] | None = None,
        **defaults: Any,
    ) -> Any:
        from skrift.agents.chat import Chat

        return Chat(self, key=key, actor=actor, deps_ref=deps_ref, defaults=defaults)

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
