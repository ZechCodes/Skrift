"""Form model base class with automatic name registration."""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import BaseModel

_form_registry: dict[str, type[BaseModel]] = {}


def camel_to_kebab(name: str) -> str:
    """Convert CamelCase to kebab-case. ContactUs -> contact-us"""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower()


def derive_form_name(cls: type) -> str:
    """Derive a form name from a class name, stripping 'Form' suffix."""
    name = cls.__name__
    if name.endswith("Form"):
        name = name[:-4]
    return camel_to_kebab(name)


def get_form_model(name: str) -> type[BaseModel]:
    """Look up a registered form model by name. Raises LookupError if not found."""
    try:
        return _form_registry[name]
    except KeyError:
        available = ", ".join(sorted(_form_registry)) or "(none)"
        raise LookupError(f"No form named '{name}'. Registered: {available}")


class FormModel(BaseModel):
    """Base class for form-backed Pydantic models.

    Usage:
        class ContactForm(FormModel, form_name="contact"):
            name: str
            email: EmailStr

    If form_name is omitted, it's derived from the class name:
        ContactForm -> "contact"
        NewsletterSignupForm -> "newsletter-signup"
    """

    _form_name: ClassVar[str]
    _form_action: ClassVar[str]
    _form_method: ClassVar[str]

    def __init_subclass__(
        cls,
        form_name: str | None = None,
        form_action: str = "",
        form_method: str = "post",
        **kwargs,
    ):
        super().__init_subclass__(**kwargs)

        if form_name is None:
            form_name = derive_form_name(cls)

        cls._form_name = form_name
        cls._form_action = form_action
        cls._form_method = form_method

        _form_registry[form_name] = cls
