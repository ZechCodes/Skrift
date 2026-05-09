"""Turn-level helpers for durable agent runs."""

from __future__ import annotations

import importlib
from enum import Enum
from typing import Any


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


def decode_turn_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Decode persisted turn kwargs before passing them to Pydantic AI."""

    run_kwargs = dict(kwargs)
    if "output_type" in run_kwargs:
        run_kwargs["output_type"] = _decode_type_ref(run_kwargs["output_type"])
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
