"""Tests for the Form class from skrift/forms/core.py."""

from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from pydantic import BaseModel

from skrift.forms.core import Form, CSRF_SESSION_KEY, CSRF_FIELD_NAME, verify_csrf, csrf_field
from skrift.forms.fields import BoundField


# -- Test models --

class SimpleForm(BaseModel):
    name: str
    email: str


class BoolForm(BaseModel):
    name: str
    agree: bool = False


class NamedModel(BaseModel):
    name: str


NamedModel._form_name = "custom-name"
NamedModel._form_action = "/submit"
NamedModel._form_method = "post"


# -- Helpers --

def make_request(session=None, form_data=None):
    request = MagicMock()
    request.session = session if session is not None else {}

    async def _form():
        return form_data or {}

    request.form = _form
    return request


def make_csrf_request(token="tok", extra_session=None, **form_fields):
    """Create a request with CSRF token pre-set in session and form data."""
    session = {CSRF_SESSION_KEY: token}
    if extra_session:
        session.update(extra_session)
    form_data = {CSRF_FIELD_NAME: token, **form_fields}
    return make_request(session=session, form_data=form_data)


# ---------------------------------------------------------------------------
# CSRF tests
# ---------------------------------------------------------------------------

class TestCSRF:
    def test_init_creates_csrf_token_if_not_present(self):
        request = make_request()
        Form(SimpleForm, request)
        assert CSRF_SESSION_KEY in request.session
        assert len(request.session[CSRF_SESSION_KEY]) > 0

    def test_init_preserves_existing_csrf_token(self):
        existing_token = "existing-token-abc"
        request = make_request(session={CSRF_SESSION_KEY: existing_token})
        Form(SimpleForm, request)
        assert request.session[CSRF_SESSION_KEY] == existing_token

    def test_csrf_field_renders_hidden_input(self):
        token = "test-token-xyz"
        request = make_request(session={CSRF_SESSION_KEY: token})
        form = Form(SimpleForm, request)
        html = form.csrf_field()
        assert f'type="hidden"' in str(html)
        assert f'name="{CSRF_FIELD_NAME}"' in str(html)
        assert f'value="{token}"' in str(html)

    @pytest.mark.asyncio
    async def test_validate_rejects_missing_csrf_token(self):
        request = make_request(
            session={},
            form_data={"name": "John", "email": "john@example.com"},
        )
        form = Form(SimpleForm, request)
        # The init sets a token in session, but form_data has no _csrf field
        result = await form.validate()
        assert result is False
        assert form.form_error is not None

    @pytest.mark.asyncio
    async def test_validate_rejects_wrong_csrf_token(self):
        token = "correct-token"
        request = make_request(
            session={CSRF_SESSION_KEY: token},
            form_data={
                CSRF_FIELD_NAME: "wrong-token",
                "name": "John",
                "email": "john@example.com",
            },
        )
        form = Form(SimpleForm, request)
        result = await form.validate()
        assert result is False

    @pytest.mark.asyncio
    async def test_validate_accepts_correct_csrf_token(self):
        request = make_csrf_request(token="test-token-123", name="John", email="john@example.com")
        form = Form(SimpleForm, request)

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            result = await form.validate()

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_rotates_token_after_successful_csrf_check(self):
        token = "original-token"
        request = make_csrf_request(token=token, name="John", email="john@example.com")
        form = Form(SimpleForm, request)

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            await form.validate()

        assert request.session[CSRF_SESSION_KEY] != token

    @pytest.mark.asyncio
    async def test_csrf_failure_sets_form_error_message(self):
        request = make_request(
            session={CSRF_SESSION_KEY: "real-token"},
            form_data={CSRF_FIELD_NAME: "bad-token", "name": "John", "email": "j@e.com"},
        )
        form = Form(SimpleForm, request)
        await form.validate()
        assert form.errors["__form__"] == "Form session expired. Please try again."

    def test_form_error_property_returns_form_level_error(self):
        request = make_request()
        form = Form(SimpleForm, request)
        form.errors["__form__"] = "Something went wrong."
        assert form.form_error == "Something went wrong."

    def test_form_error_property_returns_none_when_no_error(self):
        request = make_request()
        form = Form(SimpleForm, request)
        assert form.form_error is None


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidation:
    @pytest.mark.asyncio
    async def test_validate_returns_true_for_valid_data(self):
        request = make_csrf_request(name="Alice", email="alice@example.com")
        form = Form(SimpleForm, request)

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            result = await form.validate()

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_returns_false_for_invalid_data(self):
        # Missing required "email" field
        request = make_csrf_request(name="Alice")
        form = Form(SimpleForm, request)
        result = await form.validate()
        assert result is False

    @pytest.mark.asyncio
    async def test_validate_populates_data_on_success(self):
        request = make_csrf_request(name="Bob", email="bob@test.com")
        form = Form(SimpleForm, request)

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            await form.validate()

        assert form.data is not None
        assert form.data.name == "Bob"
        assert form.data.email == "bob@test.com"

    @pytest.mark.asyncio
    async def test_validate_populates_errors_on_failure(self):
        request = make_csrf_request(name="Alice")
        form = Form(SimpleForm, request)
        await form.validate()
        assert "email" in form.errors

    @pytest.mark.asyncio
    async def test_validate_preserves_submitted_values(self):
        request = make_csrf_request(name="Alice")
        form = Form(SimpleForm, request)
        await form.validate()
        assert form._values["name"] == "Alice"
        # CSRF field should be excluded from _values
        assert CSRF_FIELD_NAME not in form._values

    @pytest.mark.asyncio
    async def test_validate_keeps_first_error_per_field(self):
        """When Pydantic reports multiple errors for a field, only the first is kept."""
        request = make_csrf_request()  # Both fields missing
        form = Form(SimpleForm, request)
        await form.validate()
        # Both fields should have exactly one error each
        assert "name" in form.errors
        assert "email" in form.errors
        assert isinstance(form.errors["name"], str)
        assert isinstance(form.errors["email"], str)

    @pytest.mark.asyncio
    async def test_value_returns_submitted_data_after_failed_validate(self):
        request = make_csrf_request(name="Alice")
        form = Form(SimpleForm, request)
        await form.validate()
        assert form.value("name") == "Alice"
        assert form.value("email") == ""

    @pytest.mark.asyncio
    async def test_bool_coercion_missing_checkbox_gets_false(self):
        # "agree" field is missing, simulating unchecked checkbox
        request = make_csrf_request(name="Alice")
        form = Form(BoolForm, request)

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            result = await form.validate()

        assert result is True
        assert form.data.agree is False

    @pytest.mark.asyncio
    async def test_is_valid_true_after_successful_validation(self):
        request = make_csrf_request(name="A", email="a@b.com")
        form = Form(SimpleForm, request)

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            await form.validate()

        assert form.is_valid is True

    @pytest.mark.asyncio
    async def test_is_valid_false_after_failed_validation(self):
        request = make_csrf_request(name="A")
        form = Form(SimpleForm, request)
        await form.validate()
        assert form.is_valid is False

    def test_is_valid_false_before_validation(self):
        request = make_request()
        form = Form(SimpleForm, request)
        assert form.is_valid is False


# ---------------------------------------------------------------------------
# Iteration and field access tests
# ---------------------------------------------------------------------------

class TestIteration:
    def test_iter_yields_bound_fields(self):
        request = make_request()
        form = Form(SimpleForm, request)
        fields = list(form)
        assert all(isinstance(f, BoundField) for f in fields)

    def test_iter_yields_fields_in_model_definition_order(self):
        request = make_request()
        form = Form(SimpleForm, request)
        field_names = [f.name for f in form]
        assert field_names == ["name", "email"]

    def test_getitem_returns_bound_field_by_name(self):
        request = make_request()
        form = Form(SimpleForm, request)
        field = form["email"]
        assert isinstance(field, BoundField)
        assert field.name == "email"

    def test_getitem_raises_key_error_for_unknown_field(self):
        request = make_request()
        form = Form(SimpleForm, request)
        with pytest.raises(KeyError):
            form["nonexistent"]

    def test_len_returns_number_of_fields(self):
        request = make_request()
        form = Form(SimpleForm, request)
        assert len(form) == 2

    def test_contains_checks_field_existence(self):
        request = make_request()
        form = Form(SimpleForm, request)
        assert "name" in form
        assert "email" in form
        assert "missing" not in form

    @pytest.mark.asyncio
    async def test_fields_property_is_reset_after_validate(self):
        request = make_request()
        form = Form(SimpleForm, request)
        # Access fields to populate cache
        original_fields = form.fields
        assert form._fields is not None

        # Validate to reset _fields
        token = "tok"
        request.session[CSRF_SESSION_KEY] = token

        async def _form():
            return {CSRF_FIELD_NAME: token, "name": "A", "email": "a@b.com"}

        request.form = _form

        with patch("skrift.lib.hooks.hooks") as mock_hooks:
            mock_hooks.apply_filters = AsyncMock(side_effect=lambda name, val, *a: val)
            await form.validate()

        # After validate(), _fields should have been set to None and rebuilt
        # The fields dict should be a new instance
        new_fields = form.fields
        assert new_fields is not original_fields


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------

class TestRendering:
    def test_render_default_includes_csrf_field(self):
        token = "render-token"
        request = make_request(session={CSRF_SESSION_KEY: token})
        form = Form(SimpleForm, request)
        html = str(form._render_default("Submit"))
        assert f'name="{CSRF_FIELD_NAME}"' in html
        assert f'value="{token}"' in html

    def test_render_default_includes_form_error_alert(self):
        request = make_request()
        form = Form(SimpleForm, request)
        form.errors["__form__"] = "Something went wrong."
        html = str(form._render_default("Submit"))
        assert '<article role="alert">' in html
        assert "Something went wrong." in html

    def test_render_default_renders_all_fields(self):
        request = make_request()
        form = Form(SimpleForm, request)
        html = str(form._render_default("Submit"))
        # Should contain label/widget for both fields
        assert "name" in html
        assert "email" in html

    def test_render_default_includes_submit_button(self):
        request = make_request()
        form = Form(SimpleForm, request)
        html = str(form._render_default("Send"))
        assert '<button type="submit">Send</button>' in html

    def test_render_default_escapes_action_url(self):
        request = make_request()
        form = Form(SimpleForm, request, action='/submit?a=1&b=<script>"hello"</script>')
        html = str(form._render_default("Submit"))
        # The action should be escaped (angle brackets, quotes)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_render_default_no_action_attr_when_empty(self):
        request = make_request()
        form = Form(SimpleForm, request)
        html = str(form._render_default("Submit"))
        assert "action=" not in html

    def test_render_default_includes_action_when_set(self):
        request = make_request()
        form = Form(SimpleForm, request, action="/my-endpoint")
        html = str(form._render_default("Submit"))
        assert 'action="/my-endpoint"' in html


# ---------------------------------------------------------------------------
# Name resolution tests
# ---------------------------------------------------------------------------

class TestNameResolution:
    def test_uses_explicit_name_kwarg_first(self):
        request = make_request()
        form = Form(NamedModel, request, name="explicit-name")
        assert form.name == "explicit-name"

    def test_falls_back_to_form_name_class_attribute(self):
        request = make_request()
        form = Form(NamedModel, request)
        assert form.name == "custom-name"

    def test_falls_back_to_derived_name_from_class(self):
        class ContactForm(BaseModel):
            name: str

        request = make_request()
        form = Form(ContactForm, request)
        assert form.name == "contact"

    def test_derived_name_camel_to_kebab(self):
        class NewsletterSignupForm(BaseModel):
            email: str

        request = make_request()
        form = Form(NewsletterSignupForm, request)
        assert form.name == "newsletter-signup"

    def test_derived_name_without_form_suffix(self):
        class UserProfile(BaseModel):
            name: str

        request = make_request()
        form = Form(UserProfile, request)
        assert form.name == "user-profile"

    def test_action_from_class_attribute(self):
        request = make_request()
        form = Form(NamedModel, request)
        assert form.action == "/submit"

    def test_action_kwarg_overrides_class_attribute(self):
        request = make_request()
        form = Form(NamedModel, request, action="/override")
        assert form.action == "/override"

    def test_method_defaults_to_post(self):
        request = make_request()
        form = Form(SimpleForm, request)
        assert form.method == "post"

    def test_method_kwarg_overrides_default(self):
        request = make_request()
        form = Form(SimpleForm, request, method="get")
        assert form.method == "get"


# ---------------------------------------------------------------------------
# Standalone CSRF function tests
# ---------------------------------------------------------------------------

class TestStandaloneCSRF:
    """Tests for verify_csrf() and csrf_field() standalone functions."""

    @pytest.mark.asyncio
    async def test_verify_csrf_accepts_valid_token(self):
        token = "valid-token"
        request = make_request(
            session={CSRF_SESSION_KEY: token},
            form_data={CSRF_FIELD_NAME: token},
        )
        result = await verify_csrf(request)
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_csrf_rejects_invalid_token(self):
        request = make_request(
            session={CSRF_SESSION_KEY: "real-token"},
            form_data={CSRF_FIELD_NAME: "wrong-token"},
        )
        result = await verify_csrf(request)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_csrf_rejects_missing_token(self):
        request = make_request(
            session={CSRF_SESSION_KEY: "real-token"},
            form_data={},
        )
        result = await verify_csrf(request)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_csrf_rejects_empty_session_token(self):
        request = make_request(
            session={},
            form_data={CSRF_FIELD_NAME: "some-token"},
        )
        result = await verify_csrf(request)
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_csrf_rotates_token_on_success(self):
        token = "original-token"
        request = make_request(
            session={CSRF_SESSION_KEY: token},
            form_data={CSRF_FIELD_NAME: token},
        )
        await verify_csrf(request)
        assert request.session[CSRF_SESSION_KEY] != token

    def test_csrf_field_generates_hidden_input(self):
        request = make_request(session={})
        html = csrf_field(request)
        assert f'type="hidden"' in str(html)
        assert f'name="{CSRF_FIELD_NAME}"' in str(html)

    def test_csrf_field_creates_token_if_missing(self):
        request = make_request(session={})
        csrf_field(request)
        assert CSRF_SESSION_KEY in request.session
        assert len(request.session[CSRF_SESSION_KEY]) > 0

    def test_csrf_field_preserves_existing_token(self):
        existing = "existing-token"
        request = make_request(session={CSRF_SESSION_KEY: existing})
        html = csrf_field(request)
        assert f'value="{existing}"' in str(html)
        assert request.session[CSRF_SESSION_KEY] == existing
