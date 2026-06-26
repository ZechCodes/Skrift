"""Skrift content system — code-declared, admin-editable page fields.

Declare a content schema with the field helpers and bind it to a key::

    from skrift.content import ContentArea, text, textarea, group, repeater

    class HomeContent(ContentArea, key="home"):
        hero_title: str = text("Hero title", default="Welcome")
        hero_copy: str = textarea("Hero copy")

The values are edited under ``/admin/content`` and persisted per key, then
hydrated back into templates as a validated model instance.
"""

from skrift.content.fields import (
    boolean,
    email,
    group,
    number,
    phone,
    repeater,
    select,
    text,
    textarea,
    url,
)
from skrift.content.parse import parse_nested_form
from skrift.content.render import build_nodes, hydrate
from skrift.content.schema import (
    ContentArea,
    ContentModel,
    content_area,
    get_content_area,
    list_content_areas,
)

# Importing registers the built-in areas (e.g. the "home" landing page).
from skrift.content import builtin  # noqa: E402,F401

__all__ = [
    "ContentArea",
    "ContentModel",
    "content_area",
    "get_content_area",
    "list_content_areas",
    "build_nodes",
    "hydrate",
    "parse_nested_form",
    "boolean",
    "email",
    "group",
    "number",
    "phone",
    "repeater",
    "select",
    "text",
    "textarea",
    "url",
]
