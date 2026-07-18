"""Skrift Agent facade — a Pydantic AI-free agent definition.

Defining an agent (``skrift.Agent(...)``) records configuration and tool specs
without importing ``pydantic_ai``. The real :class:`pydantic_ai.Agent` is built
lazily, on first run, via :mod:`skrift.agents._materialize` — i.e. only in the
process that actually executes the agent (the worker, or inline in tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from skrift.agents.config import get_agents_config
from skrift.agents.context import current_session_id, resolve_actor
from skrift.agents.helpers import callable_name
from skrift.agents.models import ResumeContext, RunState, ToolDisplayContext, ToolPolicy
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

if TYPE_CHECKING:
    from skrift.agents._materialize import MaterializedAgent

ToolDisplayFormatter = Callable[[ToolDisplayContext], Any]


@dataclass(frozen=True)
class ToolFormatters:
    called: ToolDisplayFormatter | None = None
    returned: ToolDisplayFormatter | None = None
    errored: ToolDisplayFormatter | None = None


@dataclass
class ToolSpec:
    """A deferred tool registration, replayed at materialization time."""

    plain: bool
    func: Callable[..., Any]
    kwargs: dict[str, Any] = field(default_factory=dict)
    approval_gate: Callable[..., Any] | None = None
    detached: bool = False


@dataclass
class CallbackSpec:
    """A deferred decorator registration (system prompt, instructions, output validator).

    Captured when the agent is defined and replayed onto the Pydantic AI agent at
    materialization time.
    """

    kind: str  # "system_prompt" | "instructions" | "output_validator"
    func: Callable[..., Any]
    kwargs: dict[str, Any] = field(default_factory=dict)


class Agent:
    """Durable Skrift agent definition.

    Constructing an ``Agent`` is free of ``pydantic_ai``: it stores the
    constructor arguments and tool specs and registers an
    :class:`~skrift.agents.registry.AgentDefinition`. The public ``run`` method
    queues a worker-backed run and returns a :class:`~skrift.agents.session.Session`;
    the worker materializes the underlying Pydantic AI agent and executes it.
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
        self._init_args = args
        self._init_kwargs = kwargs
        self.skrift_name = name
        self.deps_factory = deps_factory
        # The declared output type (without the durable DeferredToolRequests
        # sentinel pydantic-ai needs); used to rehydrate persisted results.
        self._output_type = kwargs.get("output_type", str)
        self._tool_policies: dict[str, ToolPolicy] = {}
        self._approval_gates: dict[str, Callable[..., Any]] = {}
        self._detached_tools: dict[str, Callable[..., Any]] = {}
        self._tool_formatters: dict[str, ToolFormatters] = {}
        self._tool_specs: list[ToolSpec] = []
        self._callback_specs: list[CallbackSpec] = []
        self._materialized: MaterializedAgent | None = None
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
        format_called: ToolDisplayFormatter | None = None,
        format_returned: ToolDisplayFormatter | None = None,
        format_errored: ToolDisplayFormatter | None = None,
        **kwargs: Any,
    ) -> Any:
        if detached:
            raise NotImplementedError(
                "detached=True is not yet supported for context tools (@agent.tool). "
                "It works for @agent.tool_plain. If your tool needs deps, either "
                "restructure it as a plain tool that takes identifying args and "
                "looks up resources internally, or wait for the context rehydration path."
            )
        return self._register_tool(
            func,
            plain=False,
            approval=approval,
            idempotent=idempotent,
            detached=detached,
            approval_on_retry=approval_on_retry,
            policy_description=policy_description,
            format_called=format_called,
            format_returned=format_returned,
            format_errored=format_errored,
            kwargs=kwargs,
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
        format_called: ToolDisplayFormatter | None = None,
        format_returned: ToolDisplayFormatter | None = None,
        format_errored: ToolDisplayFormatter | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._register_tool(
            func,
            plain=True,
            approval=approval,
            idempotent=idempotent,
            detached=detached,
            approval_on_retry=approval_on_retry,
            policy_description=policy_description,
            format_called=format_called,
            format_returned=format_returned,
            format_errored=format_errored,
            kwargs=kwargs,
        )

    def system_prompt(
        self,
        func: Any = None,
        /,
        *,
        dynamic: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Register a system prompt function (proxies ``pydantic_ai.Agent.system_prompt``).

        Usable bare (``@agent.system_prompt``) or called (``@agent.system_prompt(dynamic=True)``).
        The function is registered with the underlying Pydantic AI agent at run time.
        """

        return self._register_callback(func, kind="system_prompt", kwargs={"dynamic": dynamic, **kwargs})

    def instructions(
        self,
        func: Any = None,
        /,
        **kwargs: Any,
    ) -> Any:
        """Register an instructions function (proxies ``pydantic_ai.Agent.instructions``).

        Usable bare (``@agent.instructions``) or called (``@agent.instructions()``).
        The function is registered with the underlying Pydantic AI agent at run time.
        """

        return self._register_callback(func, kind="instructions", kwargs=kwargs)

    def output_validator(self, func: Any = None, /) -> Any:
        """Register an output validator (proxies ``pydantic_ai.Agent.output_validator``).

        Usable bare (``@agent.output_validator``). The validator may take
        ``RunContext`` as its first argument and raise ``ModelRetry`` to ask the
        model to try again. It is registered with the underlying Pydantic AI agent
        at run time.
        """

        return self._register_callback(func, kind="output_validator", kwargs={})

    def _register_callback(self, func: Any, *, kind: str, kwargs: dict[str, Any]) -> Any:
        def record(resolved_func: Any) -> Any:
            self._callback_specs.append(CallbackSpec(kind=kind, func=resolved_func, kwargs=kwargs))
            return resolved_func

        if func is not None:
            return record(func)
        return record

    def _register_tool(
        self,
        func: Any,
        *,
        plain: bool,
        approval: bool | Callable[..., bool],
        idempotent: bool,
        detached: bool,
        approval_on_retry: bool,
        policy_description: str | None,
        format_called: ToolDisplayFormatter | None,
        format_returned: ToolDisplayFormatter | None,
        format_errored: ToolDisplayFormatter | None,
        kwargs: dict[str, Any],
    ) -> Any:
        metadata = dict(kwargs.pop("metadata", {}) or {})
        policy_approval, approval_mode, approval_callable_name, approval_gate = (
            self._configure_approval(approval, kwargs)
        )
        metadata["skrift_policy"] = ToolPolicy(
            approval=policy_approval,
            approval_mode=approval_mode,
            approval_callable_name=approval_callable_name,
            idempotent=idempotent,
            detached=detached,
            approval_on_retry=approval_on_retry,
            policy_description=policy_description,
            format_called_name=callable_name(format_called) if format_called else None,
            format_returned_name=callable_name(format_returned) if format_returned else None,
            format_errored_name=callable_name(format_errored) if format_errored else None,
        ).model_dump(mode="json")
        formatters = ToolFormatters(
            called=format_called,
            returned=format_returned,
            errored=format_errored,
        )
        tool_kwargs = {**kwargs, "metadata": metadata}

        def record(resolved_func: Any) -> Any:
            name = kwargs.get("name") or getattr(resolved_func, "__name__", "")
            self._record_tool_policy(name, metadata["skrift_policy"])
            self._record_tool_formatters(name, formatters)
            if approval_gate is not None:
                self._record_approval_gate(name, approval_gate)
            if detached:
                self._record_detached_tool(name, resolved_func)
            self._tool_specs.append(
                ToolSpec(
                    plain=plain,
                    func=resolved_func,
                    kwargs=tool_kwargs,
                    approval_gate=approval_gate,
                    detached=detached,
                )
            )
            return resolved_func

        if func is not None:
            return record(func)
        return record

    def _configure_approval(
        self,
        approval: bool | Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> tuple[bool, str, str | None, Callable[..., Any] | None]:
        if callable(approval):
            gate = approval
            kwargs["requires_approval"] = False
            return False, "callable", callable_name(gate), gate
        if approval and "requires_approval" not in kwargs:
            kwargs["requires_approval"] = True
        return bool(approval), "static" if approval else "none", None, None

    def _record_tool_policy(self, name: str, policy: dict[str, Any]) -> None:
        if name:
            self._tool_policies[name] = ToolPolicy.model_validate(policy)

    def _record_tool_formatters(self, name: str, formatters: ToolFormatters) -> None:
        if name and any((formatters.called, formatters.returned, formatters.errored)):
            self._tool_formatters[name] = formatters

    def _record_detached_tool(self, name: str, func: Callable[..., Any]) -> None:
        if name:
            self._detached_tools[name] = func

    def _record_approval_gate(self, name: str, gate: Callable[..., Any]) -> None:
        if name:
            self._approval_gates[name] = gate

    @property
    def model(self) -> Any:
        """The configured model, as passed at definition (positional or ``model=``)."""

        if self._init_args:
            return self._init_args[0]
        return self._init_kwargs.get("model")

    @property
    def materialized(self) -> MaterializedAgent:
        """The live Pydantic AI agent, built lazily on first access."""

        if self._materialized is None:
            try:
                from skrift.agents._materialize import materialize
            except ModuleNotFoundError as exc:
                if exc.name != "pydantic_ai" and not (exc.name or "").startswith("pydantic_ai"):
                    raise
                raise ModuleNotFoundError(
                    "Running a Skrift agent requires the agent runtime. Install the "
                    "'agents' extra (e.g. `pip install skrift[agents]`) in the process "
                    "that executes agents (the worker)."
                ) from exc
            self._materialized = materialize(self)
        return self._materialized

    async def _run_pydantic(self, *args: Any, **kwargs: Any) -> Any:
        return await self.materialized._run_pydantic(*args, **kwargs)

    def _iter_pydantic(self, *args: Any, **kwargs: Any) -> Any:
        return self.materialized._iter_pydantic(*args, **kwargs)

    def definition_snapshot(self) -> dict[str, Any]:
        return self.materialized.definition_snapshot()

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
        config = get_agents_config()
        dispatch = dispatch or config.default_subagent_dispatch
        if dispatch not in {"queued", "inline", "inline_then_queued", "same_worker"}:
            raise ValueError(
                "dispatch must be 'queued', 'inline', 'inline_then_queued', or 'same_worker'"
            )
        from skrift.agents.handlers import register_agent_handlers

        register_agent_handlers()
        resolved = resolve_actor(actor)
        sid = session_id or new_session_id()
        job_id = uuid4().hex
        turn_id = uuid4().hex
        if self.deps_factory is not None and "deps" in kwargs:
            raise AgentSessionError(
                f"Agent {self.skrift_name!r} uses deps_factory; pass durable "
                "dependencies through deps_ref=..., not deps=."
            )
        if deps_ref is not None and self.deps_factory is None:
            raise AgentSessionError(
                f"Agent {self.skrift_name!r} has no deps_factory; deps_ref would be ignored."
            )
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
        inline_dispatch = dispatch in {"inline", "inline_then_queued", "same_worker"}
        if not inline_dispatch:
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
        if inline_dispatch:
            from skrift.agents.models import AgentRunJob
            from skrift.workers import get_runtime

            await get_runtime().submit_inline(
                AgentRunJob(session_id=sid, agent_name=self.skrift_name),
                queue=(
                    config.default_queue
                    if dispatch == "inline_then_queued"
                    else config.priority_queue
                ),
                job_id=job_id,
                metadata={
                    "skrift_dispatch": "inline_then_queued"
                    if dispatch == "inline_then_queued"
                    else "inline"
                },
            )
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
