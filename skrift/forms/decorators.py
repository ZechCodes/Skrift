"""Decorator alternative to FormModel subclassing."""

from __future__ import annotations

from skrift.forms.model import _form_registry, derive_form_name


def form(name: str | None = None, *, action: str = "", method: str = "post"):
    """Register a plain BaseModel as a named form.

    Usage:
        @form("contact")
        class ContactForm(BaseModel):
            name: str
            email: EmailStr

    If name is omitted, derived from class name (same as FormModel).
    """

    def decorator(cls):
        form_name = name
        if form_name is None:
            form_name = derive_form_name(cls)

        cls._form_name = form_name
        cls._form_action = action
        cls._form_method = method

        _form_registry[form_name] = cls
        return cls

    return decorator
