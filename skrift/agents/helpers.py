"""Pure helpers shared between the agent facade and its materialized form.

Nothing here imports ``pydantic_ai`` — these helpers run both when defining an
agent (in the site process) and when materializing it (in the worker).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable


def callable_name(value: Callable[..., Any]) -> str:
    return getattr(value, "__name__", None) or repr(value)


def accepts_approval_context(value: Callable[..., Any]) -> bool:
    try:
        return "ctx" in inspect.signature(value).parameters
    except (TypeError, ValueError):
        return False
