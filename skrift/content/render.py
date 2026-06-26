"""Turn a content schema plus saved values into a render tree for the admin.

``build_nodes`` walks a schema's fields and emits plain dicts the admin edit
template iterates over to render widgets. Repeater rows carry a blank
``template_nodes`` row (named with an ``__INDEX__`` placeholder) that the
client clones to add new entries.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

# Sentinel used in repeater item names; the admin's JS swaps it for a real
# index when cloning a blank row.
INDEX_PLACEHOLDER = "__INDEX__"


def hydrate(schema: type[BaseModel], data: dict[str, Any] | None) -> BaseModel:
    """Validate stored ``data`` against ``schema``, filling defaults.

    Missing fields fall back to their declared defaults and unknown stored
    keys are ignored, so a schema can evolve without invalidating old content.
    """
    return schema(**(data or {}))


def build_nodes(
    schema: type[BaseModel], data: dict[str, Any] | None, prefix: str = ""
) -> list[dict[str, Any]]:
    """Build render nodes for ``schema`` populated from ``data``.

    ``data`` is expected to be a fully-defaulted dict (e.g. from
    ``hydrate(...).model_dump()``); any missing scalar falls back to its
    field default so blank repeater templates render cleanly.
    """
    values = data or {}
    nodes: list[dict[str, Any]] = []

    for name, info in schema.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        widget = extra.get("widget", "text")
        label = extra.get("label") or name.replace("_", " ").title()
        help_text = extra.get("help_text")
        full_name = f"{prefix}{name}"

        if widget == "group":
            nodes.append({
                "kind": "group",
                "label": label,
                "help_text": help_text,
                "children": build_nodes(
                    extra["schema"], values.get(name) or {}, f"{full_name}."
                ),
            })
            continue

        if widget == "repeater":
            item_schema = extra["schema"]
            rows = [
                build_nodes(item_schema, row or {}, f"{full_name}.{index}.")
                for index, row in enumerate(values.get(name) or [])
            ]
            nodes.append({
                "kind": "repeater",
                "label": label,
                "help_text": help_text,
                "name": full_name,
                "item_label": extra.get("item_label", "Item"),
                "min_items": extra.get("min_items", 0),
                "max_items": extra.get("max_items"),
                "rows": rows,
                "template_nodes": build_nodes(
                    item_schema, {}, f"{full_name}.{INDEX_PLACEHOLDER}."
                ),
            })
            continue

        nodes.append({
            "kind": "field",
            "name": full_name,
            "widget": widget,
            "label": label,
            "help_text": help_text,
            "value": values.get(name, _default_value(info)),
            "input_type": extra.get("input_type", "text"),
            "placeholder": extra.get("placeholder", ""),
            "rows": extra.get("rows", 6),
            "choices": extra.get("choices", []),
        })

    return nodes


def _default_value(info: Any) -> Any:
    """Resolve a scalar field's default, or empty string if it has none."""
    default = info.get_default(call_default_factory=True)
    if default is PydanticUndefined or default is None:
        return ""
    return default
