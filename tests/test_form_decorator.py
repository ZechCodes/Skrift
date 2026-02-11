"""Tests for the @form() decorator from skrift.forms.decorators."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from skrift.forms.decorators import form
from skrift.forms.model import _form_registry, get_form_model


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Save and restore _form_registry around each test."""
    saved = _form_registry.copy()
    yield
    _form_registry.clear()
    _form_registry.update(saved)


def test_explicit_name_registers():
    """@form("name") registers a plain BaseModel with explicit name."""

    @form("contact")
    class ContactForm(BaseModel):
        name: str
        email: str

    assert "contact" in _form_registry
    assert _form_registry["contact"] is ContactForm


def test_derived_name_without_explicit():
    """@form() without name derives from class name (ContactForm -> 'contact')."""

    @form()
    class ContactForm(BaseModel):
        name: str

    assert "contact" in _form_registry
    assert _form_registry["contact"] is ContactForm


def test_derived_name_multi_word():
    """Derived name converts CamelCase to kebab-case and strips Form suffix."""

    @form()
    class NewsletterSignupForm(BaseModel):
        email: str

    assert "newsletter-signup" in _form_registry


def test_decorated_class_still_validates():
    """Decorated class still works as a normal Pydantic model (validation)."""

    @form("validation-test")
    class UserForm(BaseModel):
        name: str
        age: int

    instance = UserForm(name="Alice", age=30)
    assert instance.name == "Alice"
    assert instance.age == 30

    with pytest.raises(ValidationError):
        UserForm(name="Alice", age="not-a-number")


def test_decorated_class_has_form_attributes():
    """Decorated class has _form_name, _form_action, _form_method attributes."""

    @form("attrs-test")
    class SomeForm(BaseModel):
        field: str

    assert SomeForm._form_name == "attrs-test"
    assert SomeForm._form_action == ""
    assert SomeForm._form_method == "post"


def test_get_form_model_finds_decorated_class():
    """Decorated class appears in get_form_model registry."""

    @form("lookup-test")
    class LookupForm(BaseModel):
        value: str

    result = get_form_model("lookup-test")
    assert result is LookupForm


def test_custom_action_and_method():
    """Custom action and method are stored on the decorated class."""

    @form("custom", action="/submit", method="put")
    class CustomForm(BaseModel):
        data: str

    assert CustomForm._form_action == "/submit"
    assert CustomForm._form_method == "put"
