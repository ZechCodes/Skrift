"""Content schema base classes and the content-area registry.

A ``ContentArea`` is a Pydantic model whose fields are declared with the
helpers in :mod:`skrift.content.fields`. Each registered area has a stable
string ``key`` used to persist its values and to expose an edit screen in the
admin. Groups and repeater items subclass :class:`ContentModel` (the same base
without registration).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

_content_registry: dict[str, type["ContentArea"]] = {}


def _humanize(key: str) -> str:
    """Turn a key like ``home-page`` into a label like ``Home Page``."""
    return key.replace("-", " ").replace("_", " ").title()


class ContentModel(BaseModel):
    """Base for content groups and repeater items.

    Unknown stored keys are ignored so a schema can drop a field without
    breaking previously saved content.
    """

    model_config = ConfigDict(extra="ignore")


class ContentArea(ContentModel):
    """A top-level, admin-editable content schema bound to a ``key``.

    Usage::

        class HomeContent(ContentArea, key="home", label="Home Page"):
            hero_title: str = text("Hero title", default="Welcome")

    Subclasses that omit ``key`` are treated as intermediate bases and are not
    registered (useful for sharing fields between areas).
    """

    _content_key: ClassVar[str]
    _content_label: ClassVar[str]
    _content_description: ClassVar[str]

    def __init_subclass__(
        cls,
        key: str | None = None,
        label: str | None = None,
        description: str = "",
        **kwargs,
    ):
        super().__init_subclass__(**kwargs)

        if key is None:
            return

        cls._content_key = key
        cls._content_label = label or _humanize(key)
        cls._content_description = description
        _content_registry[key] = cls


def content_area(
    key: str, *, label: str | None = None, description: str = ""
):
    """Register a plain :class:`ContentModel`/``BaseModel`` as a content area.

    Decorator alternative to subclassing ``ContentArea`` with ``key=``.
    """

    def decorator(cls: type[ContentModel]) -> type[ContentModel]:
        cls._content_key = key
        cls._content_label = label or _humanize(key)
        cls._content_description = description
        _content_registry[key] = cls
        return cls

    return decorator


def get_content_area(key: str) -> type[ContentArea]:
    """Look up a registered content area by key. Raises ``LookupError`` if absent."""
    try:
        return _content_registry[key]
    except KeyError:
        available = ", ".join(sorted(_content_registry)) or "(none)"
        raise LookupError(f"No content area named '{key}'. Registered: {available}")


def list_content_areas() -> dict[str, type[ContentArea]]:
    """All registered content areas by key.

    An area only appears once the module that declares it has been imported.
    """
    return dict(_content_registry)
