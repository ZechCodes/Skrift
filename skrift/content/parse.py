"""Parse dotted form field names into nested data for content validation.

The admin content form names inputs with dotted paths so nested groups and
repeater rows round-trip through a flat URL-encoded body::

    hero_title          -> {"hero_title": ...}
    cta.label           -> {"cta": {"label": ...}}
    sections.0.heading  -> {"sections": [{"heading": ...}]}

Numeric path segments become list positions. Indices may be sparse (the admin
deletes repeater rows without renumbering the survivors); they are sorted and
compacted into a dense list, so order is preserved and gaps are dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def parse_nested_form(data: Mapping[str, Any]) -> dict[str, Any]:
    """Build a nested dict/list structure from dotted form keys.

    Keys beginning with ``_`` (e.g. the CSRF token) are ignored.
    """
    root: dict[str, Any] = {}
    for raw_key, value in data.items():
        if raw_key.startswith("_"):
            continue
        _assign(root, raw_key.split("."), value)
    return _compact(root)


def _assign(root: dict[str, Any], segments: list[str], value: Any) -> None:
    """Place ``value`` into ``root`` following ``segments``, creating dicts."""
    node = root
    for segment in segments[:-1]:
        child = node.get(segment)
        if not isinstance(child, dict):
            child = {}
            node[segment] = child
        node = child
    node[segments[-1]] = value


def _compact(node: Any) -> Any:
    """Convert all-numeric-keyed dicts into index-ordered lists, recursively."""
    if not isinstance(node, dict):
        return node
    if node and all(key.isdigit() for key in node):
        return [_compact(node[key]) for key in sorted(node, key=int)]
    return {key: _compact(value) for key, value in node.items()}
