"""Agent registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from skrift.agents.models import ResumeContext, ToolPolicy


@dataclass
class AgentDefinition:
    name: str
    agent: Any
    deps_factory: Callable[[ResumeContext], Any] | None = None
    tool_policies: dict[str, ToolPolicy] = field(default_factory=dict)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, definition: AgentDefinition) -> None:
        if definition.name in self._agents and self._agents[definition.name].agent is not definition.agent:
            raise ValueError(f"Agent already registered for name {definition.name!r}")
        self._agents[definition.name] = definition

    def get(self, name: str) -> AgentDefinition:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise KeyError(f"No Skrift agent registered for name {name!r}") from exc

    def list(self) -> list[AgentDefinition]:
        return [self._agents[name] for name in sorted(self._agents)]

    def clear(self) -> None:
        self._agents.clear()


registry = AgentRegistry()
