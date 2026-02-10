"""Tests for the FormModel base class, form registry, and name derivation utilities."""

import pytest
from pydantic import ValidationError

from skrift.forms.model import (
    FormModel,
    _form_registry,
    camel_to_kebab,
    derive_form_name,
    get_form_model,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Save and restore the form registry around each test."""
    saved = _form_registry.copy()
    _form_registry.clear()
    yield
    _form_registry.clear()
    _form_registry.update(saved)


class TestCamelToKebab:
    """Test camel_to_kebab conversion edge cases."""

    def test_single_word(self):
        assert camel_to_kebab("Contact") == "contact"

    def test_already_lowercase(self):
        assert camel_to_kebab("contact") == "contact"

    def test_two_words(self):
        assert camel_to_kebab("ContactUs") == "contact-us"

    def test_multi_word(self):
        assert camel_to_kebab("NewsletterSignup") == "newsletter-signup"

    def test_acronym(self):
        """Acronyms like HTTPS stay together since splits only happen at lower->upper boundaries."""
        result = camel_to_kebab("HTTPSForm")
        # The regex splits on [a-z0-9][A-Z] boundaries, so HTTPS has no split
        # but the S->F boundary is uppercase->uppercase so no split either.
        # Only lower-to-upper or digit-to-upper boundaries trigger a split.
        assert result == "httpsform"

    def test_number_in_name(self):
        """Numbers followed by uppercase trigger a split."""
        assert camel_to_kebab("Step2Form") == "step2-form"

    def test_multiple_numbers(self):
        assert camel_to_kebab("V2Beta3Release") == "v2-beta3-release"

    def test_empty_string(self):
        assert camel_to_kebab("") == ""

    def test_single_char(self):
        assert camel_to_kebab("A") == "a"


class TestDeriveFormName:
    """Test derive_form_name strips 'Form' suffix and converts to kebab-case."""

    def test_strips_form_suffix(self):
        class ContactForm:
            pass

        assert derive_form_name(ContactForm) == "contact"

    def test_no_form_suffix(self):
        class Newsletter:
            pass

        assert derive_form_name(Newsletter) == "newsletter"

    def test_multi_word_with_form_suffix(self):
        class NewsletterSignupForm:
            pass

        assert derive_form_name(NewsletterSignupForm) == "newsletter-signup"

    def test_just_form(self):
        """A class named exactly 'Form' strips to empty string."""

        class Form:
            pass

        assert derive_form_name(Form) == ""

    def test_multi_word_no_suffix(self):
        class ContactUs:
            pass

        assert derive_form_name(ContactUs) == "contact-us"


class TestFormModelSubclass:
    """Test FormModel subclass registration and class parameter storage."""

    def test_explicit_form_name_registers(self):
        class MyForm(FormModel, form_name="custom-name"):
            name: str

        assert _form_registry["custom-name"] is MyForm
        assert MyForm._form_name == "custom-name"

    def test_derived_name_from_class_name(self):
        """ContactForm -> 'contact'."""

        class ContactForm(FormModel):
            name: str

        assert _form_registry["contact"] is ContactForm
        assert ContactForm._form_name == "contact"

    def test_multi_word_derived_kebab(self):
        """NewsletterSignupForm -> 'newsletter-signup'."""

        class NewsletterSignupForm(FormModel):
            email: str

        assert _form_registry["newsletter-signup"] is NewsletterSignupForm
        assert NewsletterSignupForm._form_name == "newsletter-signup"

    def test_form_action_default(self):
        class FeedbackForm(FormModel):
            message: str

        assert FeedbackForm._form_action == ""

    def test_form_action_custom(self):
        class FeedbackForm(FormModel, form_action="/submit-feedback"):
            message: str

        assert FeedbackForm._form_action == "/submit-feedback"

    def test_form_method_default(self):
        class FeedbackForm(FormModel):
            message: str

        assert FeedbackForm._form_method == "post"

    def test_form_method_custom(self):
        class SearchForm(FormModel, form_method="get"):
            query: str

        assert SearchForm._form_method == "get"

    def test_all_class_params(self):
        class FullForm(
            FormModel,
            form_name="full",
            form_action="/full-submit",
            form_method="put",
        ):
            data: str

        assert FullForm._form_name == "full"
        assert FullForm._form_action == "/full-submit"
        assert FullForm._form_method == "put"

    def test_form_name_not_in_model_fields(self):
        """ClassVar fields should not appear in Pydantic model_fields."""

        class SimpleForm(FormModel):
            name: str

        assert "_form_name" not in SimpleForm.model_fields
        assert "_form_action" not in SimpleForm.model_fields
        assert "_form_method" not in SimpleForm.model_fields

    def test_multiple_subclasses_register_independently(self):
        class AlphaForm(FormModel):
            a: str

        class BetaForm(FormModel):
            b: str

        class GammaForm(FormModel, form_name="custom-gamma"):
            c: str

        assert _form_registry["alpha"] is AlphaForm
        assert _form_registry["beta"] is BetaForm
        assert _form_registry["custom-gamma"] is GammaForm
        assert len(_form_registry) == 3


class TestGetFormModel:
    """Test get_form_model lookup."""

    def test_returns_registered_model(self):
        class LoginForm(FormModel):
            username: str
            password: str

        result = get_form_model("login")
        assert result is LoginForm

    def test_raises_lookup_error_for_unknown(self):
        with pytest.raises(LookupError, match="No form named 'nonexistent'"):
            get_form_model("nonexistent")

    def test_error_message_lists_available_when_empty(self):
        with pytest.raises(LookupError, match=r"\(none\)"):
            get_form_model("missing")

    def test_error_message_lists_available_forms(self):
        class AForm(FormModel, form_name="aaa"):
            x: str

        class BForm(FormModel, form_name="bbb"):
            x: str

        with pytest.raises(LookupError, match="aaa, bbb"):
            get_form_model("missing")


class TestPydanticValidation:
    """Test that Pydantic validation works normally on FormModel subclasses."""

    def test_valid_data(self):
        class ContactForm(FormModel):
            name: str
            email: str

        instance = ContactForm(name="Alice", email="alice@example.com")
        assert instance.name == "Alice"
        assert instance.email == "alice@example.com"

    def test_missing_required_field_raises(self):
        class ContactForm(FormModel):
            name: str
            email: str

        with pytest.raises(ValidationError):
            ContactForm(name="Alice")

    def test_extra_fields_behavior(self):
        class StrictForm(FormModel):
            name: str

        instance = StrictForm(name="Bob")
        assert instance.name == "Bob"

    def test_field_with_default(self):
        class OptionalFieldForm(FormModel):
            name: str
            message: str = "Hello"

        instance = OptionalFieldForm(name="Alice")
        assert instance.message == "Hello"

    def test_model_dump(self):
        class DumpForm(FormModel):
            name: str
            age: int

        instance = DumpForm(name="Alice", age=30)
        data = instance.model_dump()
        assert data == {"name": "Alice", "age": 30}
        assert "_form_name" not in data
