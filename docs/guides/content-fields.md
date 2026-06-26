# Content Fields

Content fields let you **declare editable fields in code** and have them appear in the admin for non-developers to edit. A template says what content it needs (a hero title, some copy, a call-to-action, a list of sections); Skrift renders an editor for those fields under **Admin → Content** and hands the saved, validated values back to your template.

It is built on the same Pydantic conventions as the [Forms](forms.md) system, stores values in a single JSON-backed table, and ships with a built-in `home` area that powers the default landing page.

## Overview

| Concept | What it is |
|---------|-----------|
| **Content area** | A named schema of editable fields, bound to a string `key` (e.g. `"home"`) |
| **Field helpers** | `text`, `textarea`, `url`, `select`, … — declare a field and its widget |
| **Group** | A nested set of fields (e.g. a button's label + link) |
| **Repeater** | A repeatable list of groups (e.g. "sections"), add/remove in the admin |
| **Storage** | Values persist per key in the `content_areas` table as JSON |
| **Admin** | Each area gets an editor at `/admin/content/{key}/edit` |

## Quick Start

### 1. Declare a content area

```python
from skrift.content import ContentArea, ContentModel, text, textarea, url, group, repeater


class CallToAction(ContentModel):
    label: str = text("Button label", default="Get started")
    url: str = url("Button link", default="/signup")


class HomeSection(ContentModel):
    heading: str = text("Heading", default="")
    body: str = textarea("Body", default="", rows=4)


class HomeContent(ContentArea, key="home", label="Home Page",
                  description="Hero and sections on the landing page."):
    hero_title: str = text("Hero title", default="Welcome")
    hero_subtitle: str = textarea("Hero subtitle", default="")
    cta: CallToAction = group(CallToAction, label="Call to action")
    sections: list[HomeSection] = repeater(HomeSection, item_label="Section")
```

The schema only registers once its module is imported (same rule as forms). Import it from a controller or your app package so it is available to the admin and your renderer.

### 2. Render the values in a template

In your controller, load and hydrate the area, then pass the model to the template:

```python
from skrift.content import get_content_area, hydrate
from skrift.db.services import content_service

schema = get_content_area("home")
saved = await content_service.get_content_data(db_session, "home")
content = hydrate(schema, saved)   # validated model, defaults filled in

return TemplateResponse("index.html", context={"content": content})
```

The template reads the fields as plain attributes:

```html
<section class="hero">
    <h1>{{ content.hero_title }}</h1>
    <p>{{ content.hero_subtitle }}</p>
    <a href="{{ content.cta.url }}">{{ content.cta.label }}</a>
</section>

{% for section in content.sections %}
<article>
    <h2>{{ section.heading }}</h2>
    <p>{{ section.body }}</p>
</article>
{% endfor %}
```

### 3. Edit in the admin

Sign in as a user with the `modify-site` permission and open **Admin → Content**. Each registered area is listed; the editor renders an input for every field, nested fieldsets for groups, and add/remove rows for repeaters. Saving validates the values through your schema and stores them.

That's the whole loop: **declare in code → edit in the admin → render in the template.**

## Field Types

All helpers live in `skrift.content`. Each accepts a `label` plus keyword options like `default`, `help_text`, and `placeholder`.

| Helper | Widget | Notes |
|--------|--------|-------|
| `text(label, …)` | single-line input | |
| `textarea(label, …, rows=6)` | multi-line input | |
| `url(label, …)` | text input (`type=url`) | accepts absolute or site-relative links |
| `email(label, …)` | text input (`type=email`) | |
| `phone(label, …)` | text input (`type=tel`) | |
| `number(label, …, default=0)` | numeric input | |
| `select(label, choices=[…], …)` | dropdown | `choices` is a list of `(value, label)` tuples |
| `boolean(label, …)` | checkbox | |
| `group(Schema, label=…)` | nested fieldset | `Schema` is a `ContentModel` subclass |
| `repeater(Schema, label=…, item_label=…)` | repeatable rows | `min_items` / `max_items` optional |

```python
from skrift.content import select, boolean, number

class Pricing(ContentModel):
    plan: str = select("Default plan",
                       choices=[("free", "Free"), ("pro", "Pro")], default="free")
    seats: int = number("Included seats", default=3)
    show_banner: bool = boolean("Show announcement banner", default=False)
```

### Groups

A group is a nested `ContentModel` — use it for fields that belong together, like a button:

```python
class CallToAction(ContentModel):
    label: str = text("Button label", default="Get started")
    url: str = url("Button link", default="/signup")

class HomeContent(ContentArea, key="home"):
    cta: CallToAction = group(CallToAction, label="Call to action")
```

In the template: `{{ content.cta.label }}`, `{{ content.cta.url }}`.

### Repeaters

A repeater is a list of a group schema. Editors can add and remove rows; rows preserve their order. Use `min_items` / `max_items` to bound the count.

```python
sections: list[HomeSection] = repeater(
    HomeSection, label="Sections", item_label="Section", max_items=8
)
```

In the template, iterate: `{% for section in content.sections %}…{% endfor %}`.

!!! note "Single level"
    Repeaters support one level of nesting (a list of groups). Deeply nested repeaters-within-repeaters are not rendered by the admin editor.

## Binding and Keys

A content area is identified by its `key`. Choose a stable, URL-safe key — it appears in the admin URL (`/admin/content/{key}/edit`) and is the storage key.

You can register an area two ways:

```python
# Subclass form
class HomeContent(ContentArea, key="home", label="Home Page"):
    ...

# Decorator form (for a plain ContentModel/BaseModel)
from skrift.content import content_area

@content_area("footer", label="Footer")
class FooterContent(ContentModel):
    blurb: str = textarea("Footer blurb", default="")
```

`label` and `description` are shown in the admin list; both default sensibly from the key.

## Persistence and Hydration

Values are stored in the `content_areas` table — one JSON document per key — via `skrift.db.services.content_service`:

```python
await content_service.get_content_data(db_session, "home")   # -> dict (or {} if unset)
await content_service.save_content_data(db_session, "home", data)
```

`hydrate(schema, data)` validates a stored dict against the schema and returns a model instance:

- **Missing fields** fall back to their declared defaults.
- **Unknown stored keys are ignored**, so you can remove a field from a schema without breaking previously saved content.

This makes schemas safe to evolve. When you add a field, existing content simply uses the new field's default until someone edits the area.

## Public API

```python
from skrift.content import (
    ContentArea, ContentModel, content_area,   # declare & register
    text, textarea, url, email, phone, number, select, boolean,  # field helpers
    group, repeater,                            # composites
    get_content_area, list_content_areas,       # registry lookups
    hydrate,                                     # validate stored data
)
```

`ContentArea`, `ContentModel`, `content_area`, `get_content_area`, and `list_content_areas` are also re-exported from the top-level `skrift` package.

## The built-in `home` area

Skrift ships a `HomeContent` area (`skrift/content/builtin.py`) that the default landing template (`index.html`) renders — a hero title, subtitle, a CTA group, and repeatable sections. The built-in `WebController` hydrates it automatically. Override `index.html` in your theme or project to change the markup; the editable fields stay the same.

See the **basic-site** demo (`demo/basic-site`) for a working example: log in with the Demo Login (check *is_admin*), edit **Admin → Content → Home Page**, and watch the landing page update.

## Permissions

The content admin is guarded by the `modify-site` permission — the same one used for site settings. Administrators have it by default.
