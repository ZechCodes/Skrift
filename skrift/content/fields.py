"""Field helpers for declaring editable content fields in code.

Each helper returns a Pydantic field carrying widget metadata in
``json_schema_extra``. The admin renderer reads that metadata to choose an
input type, label, and validation hints, while Pydantic itself handles
coercion and defaults. A page author declares a content schema like::

    class HomeContent(ContentArea, key="home"):
        hero_title: str = text("Hero title", default="Welcome")
        hero_copy: str = textarea("Hero copy")
        cta: CallToAction = group(CallToAction, label="Call to action")
        sections: list[Section] = repeater(Section, label="Sections")
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _content_field(
    widget: str,
    label: str | None,
    default: Any,
    help_text: str | None,
    placeholder: str | None,
    extra: dict[str, Any] | None = None,
) -> Any:
    """Build a Pydantic field annotated with content widget metadata."""
    schema_extra: dict[str, Any] = {"widget": widget}
    if label is not None:
        schema_extra["label"] = label
    if help_text is not None:
        schema_extra["help_text"] = help_text
    if placeholder is not None:
        schema_extra["placeholder"] = placeholder
    if extra:
        schema_extra.update(extra)
    return Field(default=default, json_schema_extra=schema_extra)


def text(
    label: str | None = None,
    *,
    default: str = "",
    help_text: str | None = None,
    placeholder: str | None = None,
) -> Any:
    """A single-line text input."""
    return _content_field("text", label, default, help_text, placeholder)


def textarea(
    label: str | None = None,
    *,
    default: str = "",
    help_text: str | None = None,
    placeholder: str | None = None,
    rows: int = 6,
) -> Any:
    """A multi-line text area."""
    return _content_field("textarea", label, default, help_text, placeholder, {"rows": rows})


def url(
    label: str | None = None,
    *,
    default: str = "",
    help_text: str | None = None,
    placeholder: str | None = None,
) -> Any:
    """A URL input. Accepts absolute or site-relative links."""
    return _content_field("text", label, default, help_text, placeholder, {"input_type": "url"})


def email(
    label: str | None = None,
    *,
    default: str = "",
    help_text: str | None = None,
    placeholder: str | None = None,
) -> Any:
    """An email address input."""
    return _content_field("text", label, default, help_text, placeholder, {"input_type": "email"})


def phone(
    label: str | None = None,
    *,
    default: str = "",
    help_text: str | None = None,
    placeholder: str | None = None,
) -> Any:
    """A telephone number input."""
    return _content_field("text", label, default, help_text, placeholder, {"input_type": "tel"})


def number(
    label: str | None = None,
    *,
    default: int = 0,
    help_text: str | None = None,
    placeholder: str | None = None,
) -> Any:
    """A numeric input."""
    return _content_field("number", label, default, help_text, placeholder, {"input_type": "number"})


def select(
    label: str | None = None,
    *,
    choices: list[tuple[str, str]],
    default: str = "",
    help_text: str | None = None,
) -> Any:
    """A dropdown of ``(value, label)`` choices."""
    return _content_field("select", label, default, help_text, None, {"choices": choices})


def boolean(
    label: str | None = None,
    *,
    default: bool = False,
    help_text: str | None = None,
) -> Any:
    """A checkbox toggle."""
    return _content_field("checkbox", label, default, help_text, None)


def group(
    schema: type[BaseModel],
    *,
    label: str | None = None,
    help_text: str | None = None,
) -> Any:
    """A nested group of fields, validated by ``schema``.

    The field annotation supplies the type for Pydantic; ``schema`` is also
    stored in the field metadata so the renderer can walk the nested fields
    without re-deriving the type from the annotation.
    """
    return Field(
        default_factory=schema,
        json_schema_extra={
            "widget": "group",
            "label": label,
            "help_text": help_text,
            "schema": schema,
        },
    )


def repeater(
    schema: type[BaseModel],
    *,
    label: str | None = None,
    help_text: str | None = None,
    item_label: str = "Item",
    min_items: int = 0,
    max_items: int | None = None,
) -> Any:
    """A repeatable list of ``schema`` groups (add/remove rows in the admin)."""
    return Field(
        default_factory=list,
        json_schema_extra={
            "widget": "repeater",
            "label": label,
            "help_text": help_text,
            "schema": schema,
            "item_label": item_label,
            "min_items": min_items,
            "max_items": max_items,
        },
    )
