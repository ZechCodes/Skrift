"""Turn-level helpers for durable agent runs."""

from __future__ import annotations

import importlib
from enum import Enum
from functools import cache
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import PydanticSerializationError, to_jsonable_python

from skrift.workers import get_runtime
from skrift.workers.memory import InMemoryStateStore


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
# (SQLAlchemy, Redis) saves as plain dicts (#235): single values, and lists of
# them.
_DATACLASS_KWARGS = ("usage_limits", "usage")
_DATACLASS_LIST_KWARGS = ("message_history", "builtin_tools")


@cache
def _dataclass_kwarg_adapters() -> dict[str, TypeAdapter[Any]]:
    """The adapters that rebuild each dataclass run kwarg, or each item of one.

    Built on first use: defining and dispatching an agent must not import
    pydantic-ai.
    """

    from pydantic_ai.builtin_tools import AbstractBuiltinTool
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.usage import RunUsage, UsageLimits

    return {
        "usage_limits": TypeAdapter(UsageLimits),
        "usage": TypeAdapter(RunUsage),
        "message_history": TypeAdapter(ModelMessage),
        "builtin_tools": TypeAdapter(AbstractBuiltinTool),
    }


def _dataclass_kwarg_values(run_kwargs: dict[str, Any]) -> list[tuple[str, Any]]:
    values = [
        (name, run_kwargs[name]) for name in _DATACLASS_KWARGS if run_kwargs.get(name) is not None
    ]
    for name in _DATACLASS_LIST_KWARGS:
        if isinstance(run_kwargs.get(name), (list, tuple)):
            values.extend((name, item) for item in run_kwargs[name])
    return values


def check_turn_kwargs_storable(run_kwargs: dict[str, Any]) -> None:
    """Raise TypeError for a run kwarg the worker would not get back as given,
    instead of storing it for the run to fail on.

    Every dispatch stores its turn's kwargs and runs them from the store, so
    this holds for inline dispatch too. The in-memory store keeps objects as
    they are, and the others save them as JSON; on any store, the worker
    rebuilds a dict given for a Pydantic AI dataclass.
    """

    store = get_runtime().state_store
    saves_json = not isinstance(store, InMemoryStateStore)
    model = run_kwargs.get("model")
    if saves_json and model is not None and not isinstance(model, str):
        raise TypeError(
            f"Pass model by name, such as 'openai:gpt-5.4-mini', not as a "
            f"{type(model).__name__} instance: {type(store).__name__} saves a "
            "turn's run kwargs as JSON, and a model object cannot be rebuilt from it."
        )
    values = [
        (name, value)
        for name, value in _dataclass_kwarg_values(run_kwargs)
        if saves_json or isinstance(value, dict)
    ]
    if not values:
        return
    adapters = _dataclass_kwarg_adapters()
    for name, value in values:
        try:
            stored = to_jsonable_python(value)
        except PydanticSerializationError:
            continue  # saving the turn raises this itself
        try:
            rebuilt = to_jsonable_python(adapters[name].validate_python(stored))
        except (ValueError, TypeError, PydanticSerializationError):
            rebuilt = None
        # A dict may leave out fields that have defaults; an object's JSON has
        # every field.
        if not (_json_includes(rebuilt, stored) if isinstance(value, dict) else rebuilt == stored):
            raise TypeError(
                f"{name} holds a {type(value).__name__} that the run would not get back "
                f"as given from {type(store).__name__}: pass the Pydantic AI object, or "
                "a dict of its fields with values of their types."
            )


def _json_includes(rebuilt: Any, given: Any) -> bool:
    """Whether ``rebuilt`` has every key of ``given``, at any depth, with the
    same value and JSON type."""

    if isinstance(given, dict):
        return isinstance(rebuilt, dict) and all(
            key in rebuilt and _json_includes(rebuilt[key], value) for key, value in given.items()
        )
    if isinstance(given, list):
        return (
            isinstance(rebuilt, list)
            and len(rebuilt) == len(given)
            and all(map(_json_includes, rebuilt, given))
        )
    return type(rebuilt) is type(given) and rebuilt == given


def decode_turn_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Decode persisted turn kwargs before passing them to Pydantic AI."""

    run_kwargs = dict(kwargs)
    if "output_type" in run_kwargs:
        run_kwargs["output_type"] = _decode_type_ref(run_kwargs["output_type"])
    # Only dicts are rebuilt: the in-memory store keeps the objects, and a list
    # may also hold values JSON cannot carry, such as builtin tool functions.
    adapters = _dataclass_kwarg_adapters()
    for name in _DATACLASS_KWARGS:
        if isinstance(run_kwargs.get(name), dict):
            run_kwargs[name] = adapters[name].validate_python(run_kwargs[name])
    for name in _DATACLASS_LIST_KWARGS:
        if isinstance(run_kwargs.get(name), (list, tuple)):
            run_kwargs[name] = [
                adapters[name].validate_python(item) if isinstance(item, dict) else item
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
