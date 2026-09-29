"""Turn-level helpers for durable agent runs."""

from __future__ import annotations

import importlib
from enum import Enum
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai.builtin_tools import AbstractBuiltinTool
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage, UsageLimits


class ReasoningLevel(str, Enum):
    """Common reasoning levels accepted by high-level agent APIs."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


def normalize_turn_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Normalize high-level Skrift turn kwargs into Pydantic AI run kwargs."""

    run_kwargs = dict(kwargs)
    reasoning = run_kwargs.pop("reasoning", None)
    if reasoning is not None:
        reasoning_value = reasoning.value if isinstance(reasoning, Enum) else str(reasoning)
        model_settings = dict(run_kwargs.get("model_settings") or {})
        model_settings["thinking"] = reasoning_value
        run_kwargs["model_settings"] = model_settings
        metadata = dict(run_kwargs.get("metadata") or {})
        metadata["skrift_reasoning"] = reasoning_value
        run_kwargs["metadata"] = metadata
    if "output_type" in run_kwargs:
        run_kwargs["output_type"] = _encode_type_ref(run_kwargs["output_type"])
    return run_kwargs


# Run kwargs holding Pydantic AI dataclasses, which a JSON state store
# (SQLAlchemy, Redis) saves as plain dicts, and the adapters that rebuild them
# (#235). Only dicts are rebuilt: the in-memory store keeps the objects, and a
# list may also hold values JSON cannot carry, such as builtin tool functions.
_DATACLASS_KWARGS: dict[str, TypeAdapter[Any]] = {
    "usage_limits": TypeAdapter(UsageLimits),
    "usage": TypeAdapter(RunUsage),
}
_DATACLASS_LIST_KWARGS: dict[str, TypeAdapter[Any]] = {
    "message_history": TypeAdapter(ModelMessage),
    "builtin_tools": TypeAdapter(AbstractBuiltinTool),
}


def decode_turn_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Decode persisted turn kwargs before passing them to Pydantic AI."""

    run_kwargs = dict(kwargs)
    if "output_type" in run_kwargs:
        run_kwargs["output_type"] = _decode_type_ref(run_kwargs["output_type"])
    for name, adapter in _DATACLASS_KWARGS.items():
        if isinstance(run_kwargs.get(name), dict):
            run_kwargs[name] = adapter.validate_python(run_kwargs[name])
    for name, adapter in _DATACLASS_LIST_KWARGS.items():
        if isinstance(run_kwargs.get(name), (list, tuple)):
            run_kwargs[name] = [
                adapter.validate_python(item) if isinstance(item, dict) else item
                for item in run_kwargs[name]
            ]
    return run_kwargs


def _encode_type_ref(value: Any) -> Any:
    if isinstance(value, type):
        return {"__skrift_type__": f"{value.__module__}:{value.__qualname__}"}
    if isinstance(value, list):
        return [_encode_type_ref(item) for item in value]
    if isinstance(value, tuple):
        return [_encode_type_ref(item) for item in value]
    return value


def _decode_type_ref(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__skrift_type__"}:
        return _import_type(value["__skrift_type__"])
    if isinstance(value, list):
        return [_decode_type_ref(item) for item in value]
    return value


def _import_type(path: str) -> type:
    module_path, qualname = path.split(":", 1)
    value: Any = importlib.import_module(module_path)
    for part in qualname.split("."):
        value = getattr(value, part)
    if not isinstance(value, type):
        raise TypeError(f"{path!r} does not resolve to a type")
    return value
