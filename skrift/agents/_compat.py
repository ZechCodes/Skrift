"""The differences between the pydantic-ai majors the agent runtime supports.

Skrift runs on pydantic-ai 1.x (from 1.89.1) and 2.x. Every difference between
them is bridged here, so the rest of the runtime is written once. Importing this
module imports ``pydantic_ai`` and checks its version, so the runtime imports it
before anything else from ``pydantic_ai``: a version outside the range fails
with one readable error rather than a missing name deep in a run.

Differences bridged:

- The built-in tools module, ``pydantic_ai.builtin_tools`` with
  ``AbstractBuiltinTool`` in 1.x, is ``pydantic_ai.native_tools`` with
  ``AbstractNativeTool`` in 2.x.
- ``Agent(...)``, ``Agent.run`` and ``Agent.iter`` take ``builtin_tools=`` in
  1.x; 2.x has no such kwarg and takes the tools as ``NativeTool``
  capabilities. Skrift accepts the tools as ``builtin_tools=`` or
  ``native_tools=`` on either major and passes them on as the installed one
  takes them (:func:`native_tool_kwargs`).
- ``AgentRun.usage`` is a method in 1.x and a property in 2.x
  (:func:`run_usage`).
- When a tool raises, a 2.x run's ``new_messages()`` ends in the empty
  ``ModelRequest`` that was to carry the tool results; a 1.x run's ends in the
  model response that called the tools (:func:`run_new_messages`).
"""

from __future__ import annotations

import re
from importlib import metadata
from typing import Any

import pydantic_ai

MIN_VERSION = (1, 89, 1)
MAX_MAJOR = 2
SUPPORTED_RANGE = ">=1.89.1,<3.0.0"


class UnsupportedPydanticAIVersion(ImportError):
    """The installed pydantic-ai is outside the range the agent runtime supports."""


def _installed_version() -> str:
    version = getattr(pydantic_ai, "__version__", None)
    if version:
        return str(version)
    for distribution in ("pydantic-ai-slim", "pydantic-ai"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"


def _release(version: str) -> tuple[int, ...]:
    match = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def check_version(version: str) -> int:
    """Return the pydantic-ai major of ``version``, or raise
    :class:`UnsupportedPydanticAIVersion` when the runtime does not support it."""

    release = _release(version)
    if not release or release < MIN_VERSION or release[0] > MAX_MAJOR:
        raise UnsupportedPydanticAIVersion(
            f"Skrift's agent runtime supports pydantic-ai {SUPPORTED_RANGE}, but "
            f"pydantic-ai {version} is installed. Install a supported version, for "
            "example with `pip install 'skrift[agents]'`."
        )
    return release[0]


VERSION = _installed_version()
MAJOR = check_version(VERSION)

from pydantic_ai.messages import ModelRequest

if MAJOR >= 2:
    from pydantic_ai.capabilities import NativeTool
    from pydantic_ai.native_tools import AbstractNativeTool
else:
    from pydantic_ai.builtin_tools import AbstractBuiltinTool as AbstractNativeTool

# The run and Agent kwargs Skrift accepts built-in (native) tools under, on
# either major.
NATIVE_TOOL_KWARGS = ("builtin_tools", "native_tools")

__all__ = [
    "MAJOR",
    "NATIVE_TOOL_KWARGS",
    "SUPPORTED_RANGE",
    "VERSION",
    "AbstractNativeTool",
    "UnsupportedPydanticAIVersion",
    "native_tool_kwargs",
    "run_new_messages",
    "run_usage",
]


def native_tool_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """``kwargs`` with the tools given as ``builtin_tools`` and ``native_tools``
    passed the way the installed pydantic-ai takes them."""

    if not any(name in kwargs for name in NATIVE_TOOL_KWARGS):
        return kwargs
    kwargs = dict(kwargs)
    tools = [tool for name in NATIVE_TOOL_KWARGS for tool in kwargs.pop(name, None) or ()]
    if MAJOR >= 2:
        if tools:
            kwargs["capabilities"] = [
                *(kwargs.get("capabilities") or ()),
                *(NativeTool(tool) for tool in tools),
            ]
    else:
        kwargs["builtin_tools"] = tools
    return kwargs


def run_usage(run: Any) -> Any:
    """The usage of an ``AgentRun`` so far."""

    usage = run.usage
    return usage() if callable(usage) else usage


def run_new_messages(run: Any) -> list[Any]:
    """The messages an ``AgentRun`` has produced, without a request it started
    and never filled."""

    messages = list(run.new_messages())
    if messages and isinstance(messages[-1], ModelRequest) and not messages[-1].parts:
        messages.pop()
    return messages
