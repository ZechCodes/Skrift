"""Tests for BoundField from skrift.forms.fields."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from markupsafe import Markup
from pydantic import BaseModel, Field, SecretStr

from skrift.forms.fields import BoundField


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SampleModel(BaseModel):
    name: str
    first_name: str = ""
    labeled: str = Field(json_schema_extra={"label": "Email Address"})
    bio: str = Field(
        default="",
        json_schema_extra={
            "widget": "textarea",
            "help_text": "Tell us about yourself",
        },
    )
    agree: bool = False
    role: str = Field(
        default="user",
        json_schema_extra={
            "widget": "select",
            "choices": [("admin", "Admin"), ("user", "User")],
        },
    )
    styled: str = Field(
        default="",
        json_schema_extra={"attrs": {"class_": "wide", "data_foo": "bar"}},
    )
    secret: SecretStr = Field(json_schema_extra={})


class MockForm:
    """Minimal form stand-in that satisfies BoundField's interface."""

    def __init__(
        self,
        model=SampleModel,
        values: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
    ):
        self.model = model
        self._values = values or {}
        self._errors = errors or {}

    def value(self, name: str) -> str:
        return self._values.get(name, "")

    def error(self, name: str) -> str | None:
        return self._errors.get(name)


def _bf(
    field_name: str,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> BoundField:
    """Shortcut to create a BoundField for *field_name* on SampleModel."""
    return BoundField(MockForm(values=values, errors=errors), field_name)


def _make_fake_email_model():
    """Create a fake model class whose model_fields has an 'email' field
    with an annotation whose __name__ is 'EmailStr', avoiding the need
    for the email-validator package."""

    # We need a type whose __name__ is "EmailStr". Using `type()` to create
    # it avoids the metaclass __name__ override that normal class defs get.
    FakeEmailStr = type("EmailStr", (), {})

    field_info = MagicMock()
    field_info.annotation = FakeEmailStr
    field_info.json_schema_extra = {}
    field_info.is_required.return_value = True

    model = MagicMock()
    model.model_fields = {"email": field_info}
    return model


# ---------------------------------------------------------------------------
# Label
# ---------------------------------------------------------------------------


class TestLabel:
    def test_defaults_to_title_cased_field_name(self):
        bf = _bf("first_name")
        assert bf.label == "First Name"

    def test_uses_json_schema_extra_label(self):
        bf = _bf("labeled")
        assert bf.label == "Email Address"


# ---------------------------------------------------------------------------
# ID
# ---------------------------------------------------------------------------


class TestId:
    def test_id_is_field_dash_name(self):
        bf = _bf("name")
        assert bf.id == "field-name"


# ---------------------------------------------------------------------------
# Value
# ---------------------------------------------------------------------------


class TestValue:
    def test_returns_empty_string_when_no_value_submitted(self):
        bf = _bf("name")
        assert bf.value == ""

    def test_returns_submitted_value(self):
        bf = _bf("name", values={"name": "Alice"})
        assert bf.value == "Alice"


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestError:
    def test_returns_none_when_no_error(self):
        bf = _bf("name")
        assert bf.error is None

    def test_returns_message_when_error_present(self):
        bf = _bf("name", errors={"name": "Name is required"})
        assert bf.error == "Name is required"


# ---------------------------------------------------------------------------
# Required
# ---------------------------------------------------------------------------


class TestRequired:
    def test_required_field(self):
        bf = _bf("name")
        assert bf.required is True

    def test_optional_field(self):
        bf = _bf("agree")
        assert bf.required is False


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpText:
    def test_returns_help_text_from_extra(self):
        bf = _bf("bio")
        assert bf.help_text == "Tell us about yourself"

    def test_returns_none_when_not_set(self):
        bf = _bf("name")
        assert bf.help_text is None


# ---------------------------------------------------------------------------
# Widget type
# ---------------------------------------------------------------------------


class TestWidgetType:
    def test_infers_text_for_str(self):
        bf = _bf("name")
        assert bf.widget_type == "text"

    def test_infers_checkbox_for_bool(self):
        bf = _bf("agree")
        assert bf.widget_type == "checkbox"

    def test_uses_explicit_widget(self):
        bf = _bf("bio")
        assert bf.widget_type == "textarea"


# ---------------------------------------------------------------------------
# Input type
# ---------------------------------------------------------------------------


class TestInputType:
    def test_maps_email_str_to_email(self):
        model = _make_fake_email_model()
        form = MockForm(model=model)
        bf = BoundField(form, "email")
        assert bf.input_type == "email"

    def test_maps_secret_str_to_password(self):
        bf = _bf("secret")
        assert bf.input_type == "password"

    def test_defaults_to_text(self):
        bf = _bf("name")
        assert bf.input_type == "text"


# ---------------------------------------------------------------------------
# widget() rendering
# ---------------------------------------------------------------------------


class TestWidgetRendering:
    def test_renders_input_for_text_field(self):
        bf = _bf("name", values={"name": "Alice"})
        html = bf.widget()
        assert isinstance(html, Markup)
        assert '<input type="text"' in html
        assert 'id="field-name"' in html
        assert 'name="name"' in html
        assert 'value="Alice"' in html

    def test_renders_textarea(self):
        bf = _bf("bio", values={"bio": "Hello"})
        html = bf.widget()
        assert "<textarea" in html
        assert 'id="field-bio"' in html
        assert 'name="bio"' in html
        assert ">Hello</textarea>" in html

    def test_renders_select_with_choices(self):
        bf = _bf("role", values={"role": "admin"})
        html = bf.widget()
        assert "<select" in html
        assert 'id="field-role"' in html
        assert 'name="role"' in html
        assert '<option value="admin" selected>Admin</option>' in html
        assert '<option value="user">User</option>' in html

    def test_renders_checkbox(self):
        bf = _bf("agree", values={"agree": "on"})
        html = bf.widget()
        assert '<input type="checkbox"' in html
        assert 'id="field-agree"' in html
        assert 'name="agree"' in html
        assert " checked" in html

    def test_checkbox_unchecked_when_empty_value(self):
        bf = _bf("agree")
        html = bf.widget()
        assert " checked" not in html

    def test_merges_override_attrs(self):
        bf = _bf("name")
        html = bf.widget(placeholder="Enter name")
        assert 'placeholder="Enter name"' in html

    def test_converts_class_underscore_to_class(self):
        bf = _bf("styled")
        html = bf.widget()
        assert 'class="wide"' in html

    def test_converts_data_foo_to_data_hyphen_foo(self):
        bf = _bf("styled")
        html = bf.widget()
        assert 'data-foo="bar"' in html


# ---------------------------------------------------------------------------
# label_tag()
# ---------------------------------------------------------------------------


class TestLabelTag:
    def test_includes_required_indicator_for_required_field(self):
        bf = _bf("name")
        html = bf.label_tag()
        assert isinstance(html, Markup)
        assert '<span class="required">*</span>' in html
        assert 'for="field-name"' in html

    def test_no_required_indicator_for_optional_field(self):
        bf = _bf("agree")
        html = bf.label_tag()
        assert '<span class="required">' not in html


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


class TestRender:
    def test_outputs_label_and_widget_and_error(self):
        bf = _bf("name", values={"name": "X"}, errors={"name": "Too short"})
        html = bf.render()
        assert isinstance(html, Markup)
        assert "<label" in html
        assert "<input" in html
        assert '<small class="error">Too short</small>' in html

    def test_includes_help_text(self):
        bf = _bf("bio")
        html = bf.render()
        assert '<small class="text-muted">Tell us about yourself</small>' in html

    def test_no_error_or_help_text_when_absent(self):
        bf = _bf("name")
        html = bf.render()
        assert '<small class="error">' not in html
        assert '<small class="text-muted">' not in html


# ---------------------------------------------------------------------------
# __str__
# ---------------------------------------------------------------------------


class TestStr:
    def test_str_returns_render_output(self):
        bf = _bf("name")
        assert str(bf) == str(bf.render())


# ---------------------------------------------------------------------------
# HTML escaping
# ---------------------------------------------------------------------------


class TestHtmlEscaping:
    def test_script_in_value_is_escaped(self):
        bf = _bf("name", values={"name": "<script>alert(1)</script>"})
        html = bf.widget()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_script_in_error_is_escaped(self):
        bf = _bf("name", errors={"name": "<script>alert(1)</script>"})
        html = bf.render()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_script_in_textarea_value_is_escaped(self):
        bf = _bf("bio", values={"bio": "<script>alert(1)</script>"})
        html = bf.widget()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
