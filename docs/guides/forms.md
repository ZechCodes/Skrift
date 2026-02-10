# Forms

Skrift includes a form system that handles CSRF protection, validation, and template rendering — all built on Pydantic models.

## Overview

| Feature | Description |
|---------|-------------|
| **CSRF Protection** | Automatic session-based tokens with single-use rotation |
| **Validation** | Pydantic-powered with field-level error messages |
| **Rendering** | Template hierarchy with programmatic fallback |
| **Hooks** | Filter validated data with `form_{name}_validated` and `form_validated` |

## Quick Start

### 1. Define a Form Model

```python
from skrift.forms import FormModel

class ContactForm(FormModel, form_name="contact"):
    name: str
    email: str
    message: str
```

### 2. Use Form in a Controller

```python
from litestar import Controller, get, post
from litestar.response import Template as TemplateResponse, Redirect
from litestar import Request
from skrift.forms import Form

class ContactController(Controller):
    path = "/contact"

    @get("/")
    async def show_form(self, request: Request) -> TemplateResponse:
        form = Form(ContactForm, request)
        return TemplateResponse("contact.html", context={"form": form})

    @post("/")
    async def handle_submit(self, request: Request) -> TemplateResponse | Redirect:
        form = Form(ContactForm, request)
        if await form.validate():
            # form.data is a validated ContactForm instance
            print(f"Message from {form.data.name}: {form.data.message}")
            return Redirect("/contact?success=1")

        # Validation failed — re-render with errors
        return TemplateResponse("contact.html", context={"form": form})
```

### 3. Render in a Template

```html
{# contact.html #}
{% extends "base.html" %}
{% block content %}
    <h1>Contact Us</h1>
    {{ form.render() }}
{% endblock %}
```

That's it. CSRF tokens, validation errors, and value repopulation are all handled automatically.

## Defining Form Models

### FormModel Subclass

The primary way to define a form is by subclassing `FormModel`:

```python
from skrift.forms import FormModel

class ContactForm(FormModel, form_name="contact", form_action="/contact", form_method="post"):
    name: str
    email: str
    message: str
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `form_name` | Auto-derived from class name | Unique identifier for the form |
| `form_action` | `""` (current URL) | The `action` attribute of the `<form>` tag |
| `form_method` | `"post"` | The `method` attribute of the `<form>` tag |

**Name auto-derivation:** If `form_name` is omitted, it's derived from the class name by stripping any `Form` suffix and converting to kebab-case:

- `ContactForm` → `"contact"`
- `NewsletterSignupForm` → `"newsletter-signup"`

### @form() Decorator Alternative

If you prefer working with plain `BaseModel` classes, use the `@form()` decorator:

```python
from pydantic import BaseModel
from skrift.forms import form

@form("contact")
class ContactForm(BaseModel):
    name: str
    email: str
    message: str

# Name auto-derived if omitted
@form()
class NewsletterSignupForm(BaseModel):
    email: str
```

The decorator sets the same metadata (`_form_name`, `_form_action`, `_form_method`) and registers the model in the form registry.

### Looking Up Registered Forms

Retrieve a registered form model by name:

```python
from skrift.forms import get_form_model

ContactForm = get_form_model("contact")
# Raises LookupError if not found
```

## Field Customization

Customize field rendering via Pydantic's `json_schema_extra`:

```python
from pydantic import Field, EmailStr
from skrift.forms import FormModel

class ContactForm(FormModel, form_name="contact"):
    name: str = Field(json_schema_extra={
        "label": "Your Name",
        "attrs": {"placeholder": "Jane Doe"},
    })
    email: EmailStr = Field(json_schema_extra={
        "label": "Email Address",
        "help_text": "We'll never share your email.",
        "attrs": {"placeholder": "jane@example.com"},
    })
    subject: str = Field(json_schema_extra={
        "widget": "select",
        "choices": [
            ("general", "General Inquiry"),
            ("support", "Technical Support"),
            ("billing", "Billing Question"),
        ],
    })
    message: str = Field(json_schema_extra={
        "widget": "textarea",
        "attrs": {"rows": "6"},
    })
    subscribe: bool = Field(default=False, json_schema_extra={
        "label": "Subscribe to newsletter",
        "help_text": "Get updates about new features.",
    })
```

### Available Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `label` | `str` | Auto from field name | Display label (e.g., `"email_address"` → `"Email Address"`) |
| `widget` | `str` | Auto from type | Widget type: `"text"`, `"textarea"`, `"select"`, `"checkbox"` |
| `input_type` | `str` | Auto from type | HTML input type: `"email"`, `"password"`, `"number"`, etc. |
| `help_text` | `str` | `None` | Help text displayed below the field |
| `choices` | `list[tuple]` | `[]` | For select widgets: `[(value, display_text), ...]` |
| `attrs` | `dict` | `{}` | Extra HTML attributes: `{"placeholder": "...", "class_": "wide"}` |

### Auto-Inference Rules

**Widget type** is inferred from the Python type:

| Python Type | Widget |
|-------------|--------|
| `bool` | `checkbox` |
| Everything else | `text` (renders `<input>`) |

**Input type** is inferred from the Python type:

| Python Type | Input Type |
|-------------|------------|
| `EmailStr` | `email` |
| `SecretStr` | `password` |
| Everything else | `text` |

### Attribute Naming

Python naming conventions are automatically converted to HTML:

- Trailing underscores are stripped: `class_` → `class`
- Underscores become hyphens: `data_id` → `data-id`

## Using Form in Controllers

### GET/POST Pattern

The standard pattern is to create a `Form` instance in both GET and POST handlers:

```python
from skrift.forms import Form
from litestar import Request
from litestar.response import Template as TemplateResponse, Redirect

@get("/")
async def show(self, request: Request) -> TemplateResponse:
    form = Form(ContactForm, request)
    return TemplateResponse("contact.html", context={"form": form})

@post("/")
async def submit(self, request: Request) -> TemplateResponse | Redirect:
    form = Form(ContactForm, request)

    if await form.validate():
        # Success — form.data is a validated ContactForm instance
        await send_email(form.data.email, form.data.message)
        return Redirect("/contact?thanks=1")

    # Failure — re-render with errors and repopulated values
    return TemplateResponse("contact.html", context={"form": form})
```

### Form Constructor

```python
form = Form(
    ContactForm,       # Pydantic model class
    request,           # Litestar Request
    name="contact",    # Override form name (optional)
    action="/submit",  # Override form action (optional)
    method="post",     # Override form method (optional)
)
```

Keyword arguments override the values set on the model class. If not provided, they're read from `_form_name`, `_form_action`, and `_form_method` on the model (set by `FormModel` or `@form()`).

### Accessing Validated Data

After a successful `validate()` call:

```python
if await form.validate():
    form.data          # Validated Pydantic model instance
    form.data.name     # Access individual fields
    form.data.email
    form.is_valid      # True
```

### Checking Errors

After a failed `validate()` call:

```python
if not await form.validate():
    form.errors         # dict[str, str] — field_name → error message
    form.error("email") # Error for specific field, or None
    form.form_error     # Non-field error (CSRF failure), or None
    form.is_valid       # False
```

## CSRF Protection

Every form includes automatic CSRF protection. No configuration required.

### How It Works

1. **Token creation**: When a `Form` is instantiated, a CSRF token is stored in the user's session (if not already present)
2. **Token embedding**: `form.csrf_field()` renders a hidden input with the token
3. **Token verification**: `form.validate()` checks the submitted token against the session token using constant-time comparison
4. **Token rotation**: After successful verification, a new token is generated (single-use tokens)

### In Templates

If you use `form.render()`, the CSRF field is included automatically. For manual form rendering:

```html
<form method="post">
    {{ form.csrf_field() }}
    {# ... your fields ... #}
    <button type="submit">Submit</button>
</form>
```

### CSRF Failure

If the token is missing or doesn't match, `validate()` returns `False` and sets a non-field error:

```python
form.form_error  # "Form session expired. Please try again."
```

In the default template, this renders as:

```html
<article role="alert">Form session expired. Please try again.</article>
```

## Validation

Validation is powered by Pydantic. All standard Pydantic validation features work — type coercion, validators, constraints, etc.

### How It Works

1. Form data is parsed from the request
2. CSRF token is verified
3. Unchecked checkboxes are injected as `False` (browsers don't submit unchecked checkboxes)
4. Data is validated against the Pydantic model
5. On success, validated data is passed through hooks
6. On failure, only the first error per field is kept

### Error Handling

```python
if not await form.validate():
    # Field-level errors
    form.errors                 # {"email": "value is not a valid email address", ...}
    form.error("email")         # "value is not a valid email address"

    # Non-field errors (CSRF, etc.)
    form.form_error             # "Form session expired. Please try again."

    # Submitted values are preserved for repopulation
    form.value("name")          # "Jane" (what the user typed)
```

### Value Repopulation

After failed validation, submitted values are automatically available for repopulating the form. The default template handles this automatically. For manual forms:

```html
<input type="text" name="name" value="{{ form.value('name') }}">
```

Or using bound fields:

```html
{{ form['name'].widget() }}  {# value is included automatically #}
```

## Template Rendering

### Automatic Rendering

The simplest approach — call `form.render()` in your template:

```html
{{ form.render() }}
{{ form.render(submit_label="Send Message") }}
```

This uses the template hierarchy to find the best template.

### Template Hierarchy

When `form.render()` is called, templates are searched in this order:

1. **`form-{name}.html`** — Form-specific template (e.g., `form-contact.html`)
2. **`form.html`** — Generic form template
3. **Programmatic fallback** — Built-in Python rendering (if no templates exist)

Each template is searched in:

1. `./templates/` (project root — your overrides)
2. `skrift/templates/` (package — defaults)

### Manual Field Rendering

For full control over layout, iterate fields or access them individually:

```html
<form method="{{ form.method }}"{% if form.action %} action="{{ form.action }}"{% endif %}>
    {{ form.csrf_field() }}

    {% if form.form_error %}
        <div class="alert alert-danger">{{ form.form_error }}</div>
    {% endif %}

    {% for field in form %}
        <div class="field-group">
            {{ field.label_tag() }}
            {{ field.widget() }}
            {% if field.error %}
                <span class="error">{{ field.error }}</span>
            {% endif %}
        </div>
    {% endfor %}

    <button type="submit">Submit</button>
</form>
```

### Accessing Individual Fields

```html
{# By name #}
{{ form['email'].label_tag() }}
{{ form['email'].widget() }}
{{ form['email'].widget(class_="large", placeholder="you@example.com") }}
{{ form['email'].error }}

{# Field properties #}
{{ form['email'].label }}       {# "Email" #}
{{ form['email'].value }}       {# submitted value #}
{{ form['email'].required }}    {# True/False #}
{{ form['email'].help_text }}   {# help text or None #}
{{ form['email'].id }}          {# "field-email" #}
```

### Render a Single Field Group

```html
{# Renders label + widget + error + help_text #}
{{ form.field('email') }}
```

## Custom Form Templates

### Per-Form Template

Create `templates/form-contact.html` to override rendering for the "contact" form only:

```html
{# templates/form-contact.html #}
<form method="{{ form.method }}" class="contact-form">
    {{ form.csrf_field() }}

    {% if form.form_error %}
        <div class="alert">{{ form.form_error }}</div>
    {% endif %}

    <div class="row">
        <div class="col">
            {{ form['name'].label_tag() }}
            {{ form['name'].widget(class_="form-input") }}
        </div>
        <div class="col">
            {{ form['email'].label_tag() }}
            {{ form['email'].widget(class_="form-input") }}
        </div>
    </div>

    {{ form['message'].label_tag() }}
    {{ form['message'].widget(class_="form-input", rows="8") }}

    {% if form['message'].error %}
        <small class="error">{{ form['message'].error }}</small>
    {% endif %}

    <button type="submit">{{ submit_label }}</button>
</form>
```

### Override Default Template

Create `templates/form.html` in your project root to override the default form template for all forms:

```html
{# templates/form.html #}
<form method="{{ form.method }}"{% if form.action %} action="{{ form.action }}"{% endif %} class="styled-form">
    {{ form.csrf_field() }}

    {% if form.form_error %}
        <div class="form-error">{{ form.form_error }}</div>
    {% endif %}

    {% for field in form %}
        <div class="form-group{% if field.error %} has-error{% endif %}">
            {{ field.label_tag() }}
            {{ field.widget() }}
            {% if field.error %}
                <small class="error">{{ field.error }}</small>
            {% endif %}
            {% if field.help_text %}
                <small class="help">{{ field.help_text }}</small>
            {% endif %}
        </div>
    {% endfor %}

    <button type="submit">{{ submit_label }}</button>
</form>
```

## Form Hooks

The form system integrates with Skrift's [hook/filter system](hooks-and-filters.md). Two filter hooks fire after successful validation, allowing you to modify the validated data before it reaches your controller.

### form_{name}_validated

Fires for a specific form only. The hook name includes the form name:

```python
from skrift.lib.hooks import filter

@filter("form_contact_validated")
async def sanitize_contact_data(data):
    # data is the validated ContactForm instance
    data.message = data.message.strip()
    return data
```

### form_validated

Fires for all forms:

```python
from skrift.lib.hooks import filter

@filter("form_validated")
async def log_form_submission(data, name):
    print(f"Form '{name}' submitted with: {data}")
    return data
```

Both hooks use `apply_filters`, so you **must return the data** (modified or not).

## Complete Example

Here's an end-to-end contact form with all features:

### Model

```python
# myapp/forms.py
from pydantic import Field, EmailStr
from skrift.forms import FormModel

class ContactForm(FormModel, form_name="contact", form_action="/contact"):
    name: str = Field(json_schema_extra={
        "label": "Your Name",
        "attrs": {"placeholder": "Jane Doe"},
    })
    email: EmailStr = Field(json_schema_extra={
        "label": "Email Address",
        "help_text": "We'll never share your email.",
    })
    subject: str = Field(json_schema_extra={
        "widget": "select",
        "choices": [
            ("general", "General Inquiry"),
            ("support", "Technical Support"),
            ("billing", "Billing Question"),
        ],
    })
    message: str = Field(json_schema_extra={
        "widget": "textarea",
        "label": "Your Message",
        "attrs": {"rows": "6", "placeholder": "How can we help?"},
    })
```

### Controller

```python
# myapp/controllers.py
from litestar import Controller, get, post, Request
from litestar.response import Template as TemplateResponse, Redirect
from skrift.forms import Form
from myapp.forms import ContactForm

class ContactController(Controller):
    path = "/contact"

    @get("/")
    async def show(self, request: Request) -> TemplateResponse:
        form = Form(ContactForm, request)
        return TemplateResponse("contact.html", context={"form": form})

    @post("/")
    async def submit(self, request: Request) -> TemplateResponse | Redirect:
        form = Form(ContactForm, request)

        if await form.validate():
            # Process the validated data
            await send_contact_email(
                name=form.data.name,
                email=form.data.email,
                subject=form.data.subject,
                message=form.data.message,
            )
            return Redirect("/contact?thanks=1")

        return TemplateResponse("contact.html", context={"form": form})
```

### Template

```html
{# templates/contact.html #}
{% extends "base.html" %}

{% block content %}
<h1>Contact Us</h1>

{% if request.query_params.get("thanks") %}
    <div class="success">Thank you! We'll be in touch.</div>
{% else %}
    {{ form.render(submit_label="Send Message") }}
{% endif %}
{% endblock %}
```

### Hook (Optional)

```python
# myapp/hooks.py
from skrift.lib.hooks import filter

@filter("form_contact_validated")
async def normalize_contact(data):
    data.message = data.message.strip()
    return data
```

## Best Practices

1. **Use FormModel for forms you define** — It automatically registers the form and provides name, action, and method metadata
2. **Use `@form()` for third-party models** — When you need form behavior on a model you didn't write
3. **Let the template hierarchy work for you** — Start with `form.render()` and only create custom templates when you need layout control
4. **Always include `{{ form.csrf_field() }}`** in custom templates — CSRF protection only works if the token is in the form
5. **Return data from hooks** — Form hooks use `apply_filters`, so always return the (modified) data
6. **Handle the redirect pattern** — Redirect after successful POST to prevent duplicate submissions

## Next Steps

- [Hooks and Filters](hooks-and-filters.md) — Learn about the extensibility system that powers form hooks
- [Custom Controllers](custom-controllers.md) — Build the controllers that use your forms
- [Custom Templates](custom-templates.md) — Understand the template hierarchy system
