"""Agent runtime configuration helpers."""

from __future__ import annotations

import importlib
from typing import Any

from skrift.config import AgentsConfig


def get_agents_config() -> AgentsConfig:
    """Return configured agent settings, falling back to defaults when unavailable."""

    try:
        from skrift.config import get_settings

        return get_settings().agents
    except Exception:
        return AgentsConfig()


def import_from_string(path: str) -> Any:
    module_path, _, name = path.partition(":")
    if not module_path or not name:
        raise ValueError(f"Import path must be in 'module:object' form, got {path!r}")
    return getattr(importlib.import_module(module_path), name)


def build_blob_store(config: AgentsConfig | None = None) -> Any:
    cls = import_from_string((config or get_agents_config()).blob_backend)
    return cls()


def configure_agent_runtime(config: AgentsConfig | None = None) -> None:
    """Apply config-backed agent runtime singletons."""

    from skrift.agents.blob import set_blob_store

    set_blob_store(build_blob_store(config))
