"""Core Form class with CSRF, validation, and template rendering."""

from __future__ import annotations

import hmac
import secrets
from typing import TypeVar, Generic, Type

from litestar import Request
from markupsafe import Markup, escape
from pydantic import BaseModel, ValidationError

from skrift.forms.fields import BoundField
from skrift.forms.model import camel_to_kebab

T = TypeVar("T", bound=BaseModel)

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FIELD_NAME = "_csrf"


async def verify_csrf(request: Request) -> bool:
    """Verify CSRF token from form data against the session token.

    Standalone version of Form.validate()'s CSRF check for use without
    a Form instance (e.g. action endpoints that only need CSRF protection).

    Returns True if the token is valid. Rotates the token on success.
    """
    form_data = await request.form()
    submitted_token = form_data.get(CSRF_FIELD_NAME, "")
    stored_token = request.session.get(CSRF_SESSION_KEY, "")

    if not stored_token or not hmac.compare_digest(str(submitted_token), str(stored_token)):
        return False

    # Rotate token after successful check (single-use)
    request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return True


def csrf_field(request: Request) -> Markup:
    """Generate a hidden CSRF input field, creating a session token if needed.

    Standalone version of Form.csrf_field() for use without a Form instance.
    """
    if CSRF_SESSION_KEY not in request.session:
        request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    token = request.session[CSRF_SESSION_KEY]
    return Markup(
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">'
    )


class Form(Generic[T]):
    """Model-based form with automatic CSRF and validation.

    Usage:
        form = Form(ContactForm, request)

        # In GET handler - just pass to template
        # In POST handler:
        if await form.validate():
            # form.data is a validated ContactForm instance
            ...
        else:
            # form.errors is populated, form.value() returns submitted values
            ...
    """

    def __init__(
        self,
        model: Type[T],
        request: Request,
        *,
        name: str | None = None,
        action: str | None = None,
        method: str | None = None,
    ):
        self.model = model
        self.request = request

        # Pull metadata from class, allow kwarg override
        self.name = name or getattr(model, "_form_name", None) or self._derive_name(model)
        self.action = action if action is not None else getattr(model, "_form_action", "")
        self.method = method or getattr(model, "_form_method", "post")

        self.data: T | None = None
        self.errors: dict[str, str] = {}
        self._values: dict[str, str] = {}
        self._fields: dict[str, BoundField] | None = None

        # Ensure CSRF token exists in session
        if CSRF_SESSION_KEY not in request.session:
            request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)

    # -- Validation --

    async def validate(self) -> bool:
        """Parse form data from request, verify CSRF token, validate with Pydantic.

        Returns True if valid. On failure, self.errors is populated.
        """
        form_data = await self.request.form()

        # Preserve submitted values for repopulation (exclude CSRF, skip non-string values)
        self._values = {
            k: v
            for k, v in form_data.items()
            if k != CSRF_FIELD_NAME and isinstance(v, str)
        }

        # Clear stale state
        self._fields = None
        self.errors = {}
        self.data = None

        # CSRF verification
        submitted_token = form_data.get(CSRF_FIELD_NAME, "")
        stored_token = self.request.session.get(CSRF_SESSION_KEY, "")

        if not stored_token or not hmac.compare_digest(str(submitted_token), str(stored_token)):
            self.errors["__form__"] = "Form session expired. Please try again."
            return False

        # Rotate token after successful CSRF check (single-use)
        self.request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)

        # Inject False for missing bool fields (unchecked checkboxes)
        validation_data = dict(self._values)
        for field_name, field_info in self.model.model_fields.items():
            if field_info.annotation is bool and field_name not in validation_data:
                validation_data[field_name] = False

        # Pydantic validation
        try:
            self.data = self.model(**validation_data)
        except ValidationError as e:
            for err in e.errors():
                field_name = str(err["loc"][0]) if err["loc"] else "__form__"
                # Only keep first error per field
                if field_name not in self.errors:
                    self.errors[field_name] = err["msg"]
            return False

        # Fire hooks - import here to avoid circular imports
        from skrift.lib.hooks import hooks

        self.data = await hooks.apply_filters(
            f"form_{self.name}_validated", self.data
        )
        self.data = await hooks.apply_filters(
            "form_validated", self.data, self.name
        )

        return True

    # -- Field access & iteration --

    @property
    def fields(self) -> dict[str, BoundField]:
        if self._fields is None:
            self._fields = {
                name: BoundField(self, name)
                for name in self.model.model_fields
            }
        return self._fields

    def __iter__(self):
        """Yields BoundField objects. Enables {% for field in form %}."""
        return iter(self.fields.values())

    def __getitem__(self, field_name: str) -> BoundField:
        """Enables {{ form['email'].widget() }}."""
        return self.fields[field_name]

    def __len__(self) -> int:
        return len(self.model.model_fields)

    def __contains__(self, field_name: str) -> bool:
        return field_name in self.model.model_fields

    # -- Value/error accessors --

    def value(self, field_name: str) -> str:
        """Get submitted value for a field (empty string if not submitted)."""
        return self._values.get(field_name, "")

    def error(self, field_name: str) -> str | None:
        """Get validation error for a field (None if no error)."""
        return self.errors.get(field_name)

    @property
    def form_error(self) -> str | None:
        """Non-field error (CSRF failure, etc.)."""
        return self.errors.get("__form__")

    @property
    def is_valid(self) -> bool:
        """True if validate() was called and succeeded."""
        return self.data is not None and not self.errors

    # -- CSRF --

    def csrf_field(self) -> Markup:
        """Render the hidden CSRF input."""
        token = self.request.session[CSRF_SESSION_KEY]
        return Markup(
            f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">'
        )

    # -- Rendering --

    def render(self, *, submit_label: str = "Submit") -> Markup:
        """Render the form using template hierarchy:
        form-{name}.html -> form.html -> programmatic fallback
        """
        from skrift.lib.template import Template

        template = Template("form", self.name)
        template_engine = self.request.app.template_engine
        rendered = template.try_render(
            template_engine,
            form=self,
            submit_label=submit_label,
        )

        if rendered is not None:
            return Markup(rendered)

        return self._render_default(submit_label)

    def field(self, field_name: str) -> Markup:
        """Render a single field group (label + widget + error)."""
        return self.fields[field_name].render()

    def _render_default(self, submit_label: str) -> Markup:
        """Programmatic fallback when no template exists."""
        html = f'<form method="{self.method}"'
        if self.action:
            html += f' action="{escape(self.action)}"'
        html += ">\n"
        html += str(self.csrf_field()) + "\n"

        if self.form_error:
            html += f'<article role="alert">{escape(self.form_error)}</article>\n'

        for bound_field in self:
            html += str(bound_field) + "\n"

        html += f'<button type="submit">{escape(submit_label)}</button>\n</form>'
        return Markup(html)

    # -- Internals --

    @staticmethod
    def _derive_name(model: type) -> str:
        """Derive form name from model class name."""
        name = model.__name__
        if name.endswith("Form"):
            name = name[:-4]
        return camel_to_kebab(name)
