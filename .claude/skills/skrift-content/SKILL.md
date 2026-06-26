---
name: skrift-content
description: "Skrift content fields — code-declared, admin-editable page content: ContentArea schemas, field/group/repeater helpers, JSON storage, and the /admin/content editor."
---

# Skrift Content Fields

Code declares the editable fields a template needs; a user edits them under **Admin → Content**; the template renders the saved, validated values. Built on the same Pydantic conventions as `/skrift-forms`. User-facing docs: `docs/guides/content-fields.md`.

## Architecture

```
skrift/content/
  fields.py    field helpers -> Pydantic Field(json_schema_extra={widget,label,...})
  schema.py    ContentModel (groups/items) + ContentArea (registered by key) + registry
  parse.py     parse_nested_form: dotted form names -> nested dict/list (sparse indices compacted)
  render.py    hydrate(schema, data) -> model; build_nodes(schema, data) -> admin render tree
  builtin.py   HomeContent (key="home") -> powers default landing index.html
  __init__.py  public API; imports builtin so areas register on import

skrift/db/models/content_area.py     ContentAreaRecord (table content_areas: key unique, data JSON)
skrift/db/services/content_service.py get_content_data / save_content_data
skrift/admin/content.py              ContentAdminController (/admin/content, guard Permission("modify-site"))
skrift/templates/admin/content/      list.html, edit.html, _fields.html (recursive macros + repeater JS)
```

Migration: `content_areas` table = `b0c1d2e3f4a5`.

## Declaring an area

```python
from skrift.content import ContentArea, ContentModel, text, textarea, url, group, repeater

class CallToAction(ContentModel):
    label: str = text("Button label", default="Get started")
    url: str = url("Button link", default="/signup")

class HomeSection(ContentModel):
    heading: str = text("Heading", default="")
    body: str = textarea("Body", default="", rows=4)

class HomeContent(ContentArea, key="home", label="Home Page", description="..."):
    hero_title: str = text("Hero title", default="Welcome")
    cta: CallToAction = group(CallToAction, label="Call to action")
    sections: list[HomeSection] = repeater(HomeSection, item_label="Section")
```

Areas register on import (like forms). `@content_area("key")` decorator works on a plain `ContentModel`.

## Field helpers (`skrift.content`)

`text`, `textarea`, `url`, `email`, `phone`, `number`, `select(choices=[(val,label),...])`, `boolean`, plus composites `group(Schema, label=…)` and `repeater(Schema, label=…, item_label=…, min_items=, max_items=)`. Each sets `json_schema_extra["widget"]` + label/help_text/placeholder; the annotation drives Pydantic validation.

## Rendering in a controller/template

```python
from skrift.content import get_content_area, hydrate
from skrift.db.services import content_service

schema = get_content_area("home")
content = hydrate(schema, await content_service.get_content_data(db_session, "home"))
# template: content.hero_title, content.cta.label, {% for s in content.sections %}
```

The built-in `WebController.index` does this for `home`. `index.html` guards with `{% if content %}`.

## Conventions & gotchas

- **Defaults everywhere**: every field needs a default so an area constructs with no data; `group` uses `default_factory=Schema`, `repeater` uses `default_factory=list`.
- **Schema evolution is safe**: `hydrate` fills missing fields from defaults and ignores unknown stored keys (Pydantic `extra="ignore"`).
- **Admin form names are dotted/namespaced**: `cta.label`, `sections.0.heading`. `parse_nested_form` rebuilds nesting; repeater indices may be sparse (rows deleted client-side) and are sorted+compacted.
- **Repeaters are single-level** (a list of groups). The edit template clones a `<template>` row, swapping `__INDEX__` for a monotonic counter; macros live in `_fields.html` (a partial, since importing macros from an `{% extends %}` template forces parent render).
- **Registration**: `ContentAdminController` is auto-added in `skrift/asgi.py load_controllers` alongside the other `AdminController` sub-controllers and re-exported from `skrift/admin/controller.py`.
- **Permission**: `modify-site` (same as site settings).

## Demo

`demo/basic-site` renders the `home` area on its landing page (`SiteController.index` passes `content`). Demo Login with *is_admin* → Admin → Content → Home Page.

## Related Skills

- **`/skrift-forms`** — shared Pydantic/`json_schema_extra` widget conventions
- **`/skrift-db`** — models, services, migrations
- **`/skrift-frontend`** — templates, themes, CSP nonces
- **`/skrift-auth`** — guards/permissions (`modify-site`)
