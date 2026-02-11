# Skrift Form System — Implementation Plan

## Overview

Add a model-based form system to Skrift that provides CSRF protection, Pydantic validation, template-hierarchy rendering, and field iteration — all with minimal developer boilerplate.

**Design goals:**

- Developers define a Pydantic model and get CSRF, validation, rendering, and error handling for free
- Forms are iterable in templates, yielding `BoundField` objects with rendering helpers
- `form.render()` uses Skrift's existing template hierarchy (`form-{name}.html` → `form.html` → programmatic fallback)
- Works with both a `FormModel` base class (preferred) and plain `BaseModel` (via decorator or manual name)
- CSRF tokens live in the existing encrypted session — no extra cookies or middleware

## Project Structure

All new code goes under `skrift/forms/`. No existing files need modification except `skrift/asgi.py` (template globals) and optionally `skrift/lib/template.py` (add `try_render`).

```
skrift/forms/
├── __init__.py          # Public API re-exports
├── model.py             # FormModel base class, registry, camel_to_kebab
├── fields.py            # BoundField class
├── core.py              # Form class (CSRF, validation, rendering, iteration)
└── decorators.py        # @form() decorator alternative

skrift/lib/template.py   # Add try_render() method to existing Template class

templates/form.html      # Default shipped form template

tests/
├── test_form_model.py   # FormModel registration, naming, __init_subclass__
├── test_bound_field.py  # BoundField rendering, properties
├── test_form_core.py    # CSRF, validation, iteration, template rendering
└── test_form_decorator.py  # @form() decorator
```

## Existing Code to Understand First

Before writing code, read these files to understand the patterns you're working with:

1. **`skrift/lib/template.py`** — The `Template` class and its resolution logic. You'll add a `try_render()` method here.
2. **`skrift/asgi.py`** — App factory. Find where template globals/engine are configured. You'll register `Form` as a Jinja2 global.
3. **`skrift/controllers/auth.py`** — See how `request.session` is used for OAuth state tokens. The CSRF token uses the same session mechanism.
4. **`skrift/config.py`** — Understand `get_settings()` and the config pattern.
5. **`skrift/lib/hooks.py`** — The hook/filter system. Forms will fire hooks on validation.
6. **`skrift/auth/guards.py`** — Example of `__init_subclass__`-like patterns and `__or__`/`__and__` operator overloading in the codebase.

## Implementation Steps

### Step 1: `skrift/forms/model.py`

The `FormModel` base class, the form registry, and the name derivation utility.

```python
"""Form model base class with automatic name registration."""

from __future__ import annotations

import re
from pydantic import BaseModel

_form_registry: dict[str, type[FormModel]] = {}


class FormModel(BaseModel):
    """
    Base class for form-backed Pydantic models.
    
    Usage:
        class ContactForm(FormModel, form_name="contact"):
            name: str
            email: EmailStr
    
    If form_name is omitted, it's derived from the class name:
        ContactForm -> "contact"
        NewsletterSignupForm -> "newsletter-signup"
    """

    # Class-level metadata (not Pydantic fields)
    _form_name: str
    _form_action: str = ""
    _form_method: str = "post"

    def __init_subclass__(
        cls,
        form_name: str | None = None,
        form_action: str = "",
        form_method: str = "post",
        **kwargs,
    ):
        super().__init_subclass__(**kwargs)

        if form_name is None:
            name = cls.__name__
            if name.endswith("Form"):
                name = name[:-4]
            form_name = camel_to_kebab(name)

        cls._form_name = form_name
        cls._form_action = form_action
        cls._form_method = form_method

        _form_registry[form_name] = cls


def get_form_model(name: str) -> type[FormModel]:
    """Look up a registered form model by name. Raises LookupError if not found."""
    try:
        return _form_registry[name]
    except KeyError:
        available = ", ".join(sorted(_form_registry)) or "(none)"
        raise LookupError(f"No form named '{name}'. Registered: {available}")


def camel_to_kebab(name: str) -> str:
    """Convert CamelCase to kebab-case. ContactUs -> contact-us"""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower()
```

**Key decisions:**

- `_form_name`, `_form_action`, `_form_method` are stored as class variables, NOT Pydantic fields. Pydantic should ignore them. Verify this works — if Pydantic complains, use `model_config = ConfigDict(ignored_types=(str,))` or store them in `__class_vars__`.
- The registry is module-level. Thread safety isn't a concern because registration happens at import time.
- `camel_to_kebab` is a standalone utility so tests can cover it directly.

### Step 2: `skrift/forms/decorators.py`

Decorator for attaching form metadata to plain `BaseModel` classes.

```python
"""Decorator alternative to FormModel subclassing."""

from __future__ import annotations

from skrift.forms.model import _form_registry, camel_to_kebab


def form(name: str | None = None, *, action: str = "", method: str = "post"):
    """
    Register a plain BaseModel as a named form.
    
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
            n = cls.__name__
            if n.endswith("Form"):
                n = n[:-4]
            form_name = camel_to_kebab(n)

        cls._form_name = form_name
        cls._form_action = action
        cls._form_method = method

        _form_registry[form_name] = cls
        return cls

    return decorator
```

**This is intentionally thin.** It just stamps the same attributes that `FormModel.__init_subclass__` does and registers the class.

### Step 3: `skrift/forms/fields.py`

The `BoundField` class — what you get when you iterate a form.

```python
"""Bound field class for template rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING
from markupsafe import Markup

if TYPE_CHECKING:
    from skrift.forms.core import Form


class BoundField:
    """
    A field bound to a form instance with current value, error state, and rendering helpers.
    
    Yielded by iterating a Form. Usable in templates as:
        {{ field }}              — full group (label + widget + error)
        {{ field.label_tag() }}  — just the label
        {{ field.widget() }}     — just the input/textarea/select
        {{ field.error }}        — error message or None
    """

    def __init__(self, form: Form, field_name: str):
        self.form = form
        self.name = field_name
        self._info = form.model.model_fields[field_name]
        self._extra: dict = self._info.json_schema_extra or {}

    # ── Properties ────────────────────────────────────────

    @property
    def id(self) -> str:
        return f"field-{self.name}"

    @property
    def label(self) -> str:
        return self._extra.get("label", self.name.replace("_", " ").title())

    @property
    def value(self) -> str:
        return self.form.value(self.name)

    @property
    def error(self) -> str | None:
        return self.form.error(self.name)

    @property
    def required(self) -> bool:
        return self._info.is_required()

    @property
    def help_text(self) -> str | None:
        return self._extra.get("help_text")

    @property
    def widget_type(self) -> str:
        explicit = self._extra.get("widget")
        if explicit:
            return explicit
        return _infer_widget(self._info)

    @property
    def input_type(self) -> str:
        """HTML input type attribute for <input> elements."""
        # Check explicit override first
        explicit = self._extra.get("input_type")
        if explicit:
            return explicit

        # Map by annotation name
        type_map = {
            "EmailStr": "email",
            "SecretStr": "password",
        }
        annotation = self._info.annotation
        if annotation is not None and hasattr(annotation, "__name__"):
            return type_map.get(annotation.__name__, "text")
        return "text"

    @property
    def attrs(self) -> dict:
        """Extra HTML attributes from json_schema_extra['attrs']."""
        return self._extra.get("attrs", {})

    # ── Rendering ─────────────────────────────────────────

    def label_tag(self) -> Markup:
        req = ' <span class="required">*</span>' if self.required else ""
        return Markup(f'<label for="{self.id}">{_escape(self.label)}{req}</label>')

    def widget(self, **override_attrs) -> Markup:
        """
        Render the input/textarea/select element.
        
        Extra keyword arguments become HTML attributes:
            {{ field.widget(class="wide", placeholder="...") }}
        """
        # Merge: model-level attrs < call-time overrides
        merged = {**self.attrs, **override_attrs}
        attrs_str = _render_attrs(merged)

        wt = self.widget_type

        if wt == "textarea":
            return Markup(
                f'<textarea id="{self.id}" name="{self.name}"{attrs_str}>'
                f"{_escape(self.value)}</textarea>"
            )

        if wt == "select":
            choices = self._extra.get("choices", [])
            html = f'<select id="{self.id}" name="{self.name}"{attrs_str}>'
            for val, display in choices:
                selected = " selected" if str(val) == self.value else ""
                html += f'<option value="{_escape(str(val))}"{selected}>{_escape(str(display))}</option>'
            html += "</select>"
            return Markup(html)

        if wt == "checkbox":
            checked = " checked" if self.value else ""
            return Markup(
                f'<input type="checkbox" id="{self.id}" '
                f'name="{self.name}"{checked}{attrs_str}>'
            )

        # Default: <input type="...">
        return Markup(
            f'<input type="{self.input_type}" id="{self.id}" '
            f'name="{self.name}" value="{_escape(self.value)}"{attrs_str}>'
        )

    def render(self) -> Markup:
        """Render label + widget + error as a complete field group."""
        html = str(self.label_tag()) + "\n" + str(self.widget())
        if self.error:
            html += f'\n<small class="error">{_escape(self.error)}</small>'
        if self.help_text:
            html += f'\n<small class="text-muted">{_escape(self.help_text)}</small>'
        return Markup(html)

    def __str__(self) -> str:
        return str(self.render())

    def __repr__(self) -> str:
        return f"BoundField({self.name!r}, value={self.value!r}, error={self.error!r})"


# ── Utilities ─────────────────────────────────────────────

def _infer_widget(field_info) -> str:
    """Infer widget type from Pydantic field annotation."""
    annotation = field_info.annotation
    if annotation is bool:
        return "checkbox"
    # Add more inference rules here as needed
    return "text"


def _escape(value: str) -> str:
    """HTML-escape a string."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_attrs(attrs: dict) -> str:
    """Render a dict as HTML attributes string. Returns '' or ' key="val" key2="val2"'."""
    if not attrs:
        return ""
    parts = []
    for k, v in attrs.items():
        # Convert Python naming to HTML: class_ -> class, data_id -> data-id
        attr_name = k.rstrip("_").replace("_", "-")
        parts.append(f'{attr_name}="{_escape(str(v))}"')
    return " " + " ".join(parts)
```

**Key decisions:**

- `widget()` accepts `**override_attrs` so templates can do `{{ field.widget(class="wide") }}`
- `_render_attrs` converts `class_` to `class` and `data_id` to `data-id` for Pythonic kwarg names
- `__str__` returns `render()` so `{{ field }}` in Jinja works as a complete field group
- `attrs` from `json_schema_extra["attrs"]` lets model authors set default HTML attributes

### Step 4: `skrift/forms/core.py`

The main `Form` class — CSRF, validation, iteration, template rendering.

```python
"""Core Form class with CSRF, validation, and template rendering."""

from __future__ import annotations

import hmac
import secrets
from typing import TypeVar, Generic, Type

from litestar import Request
from markupsafe import Markup
from pydantic import BaseModel, ValidationError

from skrift.forms.fields import BoundField, _escape
from skrift.forms.model import camel_to_kebab

T = TypeVar("T", bound=BaseModel)

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FIELD_NAME = "_csrf"


class Form(Generic[T]):
    """
    Model-based form with automatic CSRF and validation.

    Usage:
        form = Form(ContactForm, request)
        
        # In GET handler — just pass to template
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

    # ── Validation ────────────────────────────────────────

    async def validate(self) -> bool:
        """
        Parse form data from request, verify CSRF token, validate with Pydantic.
        Returns True if valid. On failure, self.errors is populated.
        """
        form_data = await self.request.form()

        # Preserve submitted values for repopulation (exclude CSRF token)
        self._values = {
            k: v for k, v in form_data.items()
            if k != CSRF_FIELD_NAME
        }

        # Clear any stale state
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

        # Pydantic validation
        try:
            self.data = self.model(**self._values)
        except ValidationError as e:
            for err in e.errors():
                field_name = str(err["loc"][0]) if err["loc"] else "__form__"
                # Only keep first error per field
                if field_name not in self.errors:
                    self.errors[field_name] = err["msg"]
            return False

        # Fire hooks — import here to avoid circular imports
        from skrift.lib.hooks import hooks
        self.data = await hooks.apply_filters(
            f"form_{self.name}_validated", self.data
        )
        self.data = await hooks.apply_filters(
            "form_validated", self.data, self.name
        )

        return True

    # ── Field access & iteration ──────────────────────────

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

    # ── Value/error accessors ─────────────────────────────

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

    # ── CSRF ──────────────────────────────────────────────

    def csrf_field(self) -> Markup:
        """Render the hidden CSRF input."""
        token = self.request.session[CSRF_SESSION_KEY]
        return Markup(
            f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">'
        )

    # ── Rendering ─────────────────────────────────────────

    def render(self, *, submit_label: str = "Submit") -> Markup:
        """
        Render the form using template hierarchy:
            form-{name}.html -> form.html -> programmatic fallback
        """
        from skrift.lib.template import Template

        template = Template("form", self.name)
        rendered = template.try_render(
            "templates",
            form=self,
            submit_label=submit_label,
        )

        if rendered is not None:
            return Markup(rendered)

        return self._render_default(submit_label)

    def field(self, field_name: str) -> Markup:
        """Render a single field group (label + widget + error). Shorthand for {{ form['name'] }}."""
        return self.fields[field_name].render()

    def _render_default(self, submit_label: str) -> Markup:
        """Programmatic fallback when no template exists."""
        html = f'<form method="{self.method}"'
        if self.action:
            html += f' action="{_escape(self.action)}"'
        html += ">\n"
        html += str(self.csrf_field()) + "\n"

        if self.form_error:
            html += f'<article role="alert">{_escape(self.form_error)}</article>\n'

        for bound_field in self:
            html += str(bound_field) + "\n"

        html += f'<button type="submit">{_escape(submit_label)}</button>\n</form>'
        return Markup(html)

    # ── Internals ─────────────────────────────────────────

    @staticmethod
    def _derive_name(model: type) -> str:
        """Derive form name from model class name."""
        name = model.__name__
        if name.endswith("Form"):
            name = name[:-4]
        return camel_to_kebab(name)
```

**Key decisions:**

- `validate()` is `async` because `request.form()` is async. Everything else is sync.
- CSRF uses `hmac.compare_digest` for constant-time comparison.
- Token is rotated AFTER successful CSRF check but BEFORE Pydantic validation. This means a failed validation still consumes the token (correct — prevents replay).
- `_fields` is lazily built and reset when `validate()` is called (so fields reflect new values).
- Hook calls use late import to avoid circular dependencies.
- Both `form.field("name")` (Markup) and `form["name"]` (BoundField) work — different return types for different use cases.

### Step 5: `skrift/forms/__init__.py`

Public API — one import gets everything.

```python
"""Skrift form system — model-based forms with CSRF and template rendering."""

from skrift.forms.core import Form
from skrift.forms.model import FormModel, get_form_model
from skrift.forms.fields import BoundField
from skrift.forms.decorators import form

__all__ = ["Form", "FormModel", "BoundField", "form", "get_form_model"]
```

### Step 6: Add `try_render()` to `skrift/lib/template.py`

Add this method to the existing `Template` class. Read the class first to understand its internals — particularly how `_candidates()` generates file names and how `render()` works. Then add:

```python
def try_render(self, template_dir: str, **context) -> str | None:
    """
    Attempt to render using the template hierarchy.
    Returns the rendered string, or None if no matching template file exists.
    
    This is used by the form system to fall back to programmatic rendering
    when no form template is defined.
    """
    for candidate in self._candidates():
        path = Path(template_dir) / candidate
        if path.exists():
            return self._render_file(path, **context)
    return None
```

**Important:** Look at how the existing `render()` method works. `try_render()` should use the same internal rendering mechanism but return `None` instead of raising when no template is found. If `_render_file` doesn't exist as a method, adapt — the goal is to reuse whatever Jinja rendering the class already does. You may need to access the Jinja environment from the Litestar app.

### Step 7: Default `form.html` template

Create `templates/form.html` — the generic form template that ships with Skrift:

```html
<form method="{{ form.method }}"{% if form.action %} action="{{ form.action }}"{% endif %}>
    {{ form.csrf_field() }}

    {% if form.form_error %}
        <article role="alert">{{ form.form_error }}</article>
    {% endif %}

    {% for field in form %}
        {{ field.label_tag() }}
        {{ field.widget() }}
        {% if field.error %}
            <small class="error">{{ field.error }}</small>
        {% endif %}
        {% if field.help_text %}
            <small class="text-muted">{{ field.help_text }}</small>
        {% endif %}
    {% endfor %}

    <button type="submit">{{ submit_label }}</button>
</form>
```

This uses Skrift's built-in CSS framework classes (see `docs/reference/css-framework.md`). No extra CSS needed.

### Step 8: Register in `skrift/asgi.py`

Find where the Jinja2 template engine is configured and add `Form` as a global:

```python
from skrift.forms import Form

# In the template engine setup:
template_engine.engine.globals["Form"] = Form
```

This allows templates to instantiate forms directly if needed, though the normal pattern is to pass them from controllers.

### Step 9: Add `form_validated` hook definitions

Add the hook constants to `skrift/lib/hooks.py` alongside the existing hook constants:

```python
# Form hooks
FORM_VALIDATED = "form_validated"
```

Document that `form_{name}_validated` is dynamic (e.g., `form_contact_validated`).

## Testing Plan

### `tests/test_form_model.py`

```
- FormModel subclass with explicit form_name registers correctly
- FormModel subclass without form_name derives name from class (ContactForm -> "contact")
- FormModel subclass with multi-word name derives kebab-case (NewsletterSignupForm -> "newsletter-signup")
- get_form_model returns registered model
- get_form_model raises LookupError for unknown name
- form_action and form_method class params are stored
- camel_to_kebab edge cases: single word, acronyms, numbers
- Multiple FormModel subclasses each register independently
- Pydantic validation still works normally on FormModel subclasses
- _form_name etc. are NOT treated as Pydantic fields (don't appear in model_fields)
```

### `tests/test_form_decorator.py`

```
- @form("name") registers a plain BaseModel
- @form() without name derives from class name
- Decorated class still works as a normal Pydantic model
- Decorated class has _form_name, _form_action, _form_method attributes
- Decorated class appears in get_form_model registry
```

### `tests/test_bound_field.py`

```
- label defaults to title-cased field name
- label uses json_schema_extra["label"] when provided
- id is "field-{name}"
- value returns empty string when no value submitted
- value returns submitted value after validate()
- error returns None when no error
- error returns message after failed validation
- required reflects Pydantic field requirement
- help_text returns json_schema_extra["help_text"] or None
- widget_type infers "text" for str
- widget_type infers "checkbox" for bool
- widget_type uses explicit json_schema_extra["widget"]
- input_type maps EmailStr to "email"
- input_type maps SecretStr to "password"
- input_type defaults to "text"
- widget() renders <input> for text fields
- widget() renders <textarea> for textarea widget
- widget() renders <select> with choices
- widget() renders <input type="checkbox"> for checkbox
- widget(**attrs) merges extra HTML attributes
- widget() converts class_ to class, data_foo to data-foo
- label_tag() includes required indicator when field is required
- render() outputs label + widget + error
- render() includes help_text when present
- __str__ returns render() output
- All rendered HTML is properly escaped (test with "<script>" in values)
```

### `tests/test_form_core.py`

```
CSRF:
- Form.__init__ creates CSRF token in session if not present
- Form.__init__ preserves existing CSRF token in session
- csrf_field() renders hidden input with current token
- validate() rejects missing CSRF token
- validate() rejects wrong CSRF token
- validate() accepts correct CSRF token
- validate() uses hmac.compare_digest (constant-time)
- validate() rotates token after successful CSRF check
- CSRF failure sets errors["__form__"] message
- form_error property returns __form__ error

Validation:
- validate() returns True for valid data
- validate() returns False for invalid data
- validate() populates self.data on success
- validate() populates self.errors on failure
- validate() maps Pydantic errors to correct field names
- validate() preserves submitted values in _values
- validate() only keeps first error per field
- After failed validate(), value() returns submitted data
- After failed validate(), fields reflect submitted values

Iteration:
- __iter__ yields BoundField for each model field
- __iter__ yields fields in model definition order
- __getitem__ returns BoundField by name
- __getitem__ raises KeyError for unknown field
- __len__ returns number of fields
- __contains__ checks field existence
- fields property is lazily built
- fields property is reset after validate()

Rendering:
- render() falls back to _render_default when no template exists
- _render_default includes CSRF field
- _render_default includes form_error alert when present
- _render_default renders all fields
- _render_default includes submit button
- _render_default escapes action URL
- field("name") returns rendered Markup for that field

Name resolution:
- Uses explicit name kwarg first
- Falls back to _form_name class attribute
- Falls back to derived name from class
- Name is available as form.name

Integration:
- Full round-trip: create form, render, submit, validate, access data
- Form works with FormModel subclass
- Form works with @form decorated BaseModel
- Form works with plain BaseModel (name passed to Form())
```

### `tests/test_template_integration.py`

```
- try_render returns None when no template file exists
- try_render returns rendered string when template exists
- Template hierarchy: form-contact.html found before form.html
- Template hierarchy: form.html used when form-contact.html missing
- Form object is iterable in Jinja template
- form.csrf_field() works in Jinja template
- BoundField methods (label_tag, widget, etc.) work in Jinja
- {{ field }} renders complete field group in Jinja
```

## Important Implementation Notes

1. **Pydantic class vars**: Verify that `_form_name`, `_form_action`, `_form_method` don't interfere with Pydantic. If they show up in `model_fields`, use `ClassVar` annotations or Pydantic's `model_config` to exclude them. Test this explicitly.

2. **`markupsafe` dependency**: Check if Litestar/Jinja2 already brings in `markupsafe`. If not, add it to dependencies. It should already be there via Jinja2.

3. **`request.form()` return type**: Litestar's `request.form()` returns form data. Verify the exact API — it may return `FormMultiDict` or similar. Values may need `str()` conversion. Handle `UploadFile` values gracefully (skip them or handle file fields separately).

4. **Template `try_render`**: The existing `Template` class may render via Litestar's Jinja2 engine rather than loading files directly. Study how it works before adding `try_render()`. The method needs access to the Jinja environment to render with the full context (template globals, filters, etc.).

5. **Escaping**: Use `markupsafe.Markup` for all rendered HTML so Jinja2 doesn't double-escape. The `_escape` utility handles the input side (user-submitted values). Test that `{{ form.csrf_field() }}` doesn't get escaped in templates.

6. **Circular imports**: `core.py` imports from `fields.py` and `model.py`. `fields.py` type-hints `Form` with `TYPE_CHECKING`. Hooks are imported lazily inside `validate()`. Keep this structure to avoid import cycles.

7. **Hook safety**: The `form_validated` hook filters should not break if no hooks are registered. Verify that `hooks.apply_filters` returns the original value when no filters exist for that hook name.
