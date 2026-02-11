---
name: skrift
description: "Help working with Skrift CMS codebases - a Python async CMS built on Litestar with WordPress-style conventions. Use for creating controllers, models, hooks, pages, and templates."
---

# Skrift CMS Development Guide

Skrift is a lightweight async Python CMS built on Litestar, featuring WordPress-style template resolution, a hook/filter extensibility system, and SQLAlchemy async database access.

## Current Project State

**Configuration:**
!`cat app.yaml 2>/dev/null || echo "No app.yaml found"`

**Controllers:**
!`ls skrift/controllers/*.py 2>/dev/null | head -10`

**Models:**
!`ls skrift/db/models/*.py 2>/dev/null | head -10`

**Services:**
!`ls skrift/db/services/*.py 2>/dev/null | head -10`

**Templates:**
!`ls templates/*.html 2>/dev/null | head -10 || echo "No custom templates"`

## Quick Reference

### Core Architecture

- **Framework**: Litestar (async Python web framework)
- **Database**: SQLAlchemy async with Advanced Alchemy
- **Templates**: Jinja2 with WordPress-style template hierarchy
- **Config**: YAML (app.yaml) + environment variables (.env)
- **Auth**: OAuth providers + role-based permissions

### Key Files

| File | Purpose |
|------|---------|
| `skrift/asgi.py` | AppDispatcher, app creation, middleware loading |
| `skrift/config.py` | Settings management, YAML config loading |
| `skrift/cli.py` | CLI commands (serve, secret, db) |
| `skrift/middleware/` | Security headers middleware |
| `skrift/lib/hooks.py` | WordPress-like hook/filter system |
| `skrift/lib/template.py` | Template resolution with fallbacks |
| `skrift/db/base.py` | SQLAlchemy Base class (UUIDAuditBase) |
| `skrift/auth/` | Guards, roles, permissions |

### CLI Commands

```bash
# Run development server
skrift serve --reload --port 8080

# Generate secret key
skrift secret                    # Print to stdout
skrift secret --write .env       # Write to .env file

# Database migrations (wraps Alembic)
skrift db upgrade head           # Apply all migrations
skrift db downgrade -1           # Rollback one migration
skrift db current                # Show current revision
skrift db revision -m "desc" --autogenerate  # Create migration
```

## Task-Specific Guidance

### Creating a Controller

Controllers are Litestar Controller classes registered in `app.yaml`:

```python
from litestar import Controller, get, post
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

class MyController(Controller):
    path = "/my-path"

    @get("/")
    async def list_items(self, db_session: AsyncSession) -> TemplateResponse:
        # db_session is injected automatically
        return TemplateResponse("my-template.html", context={"items": []})
```

Register in `app.yaml`:
```yaml
controllers:
  - myapp.controllers:MyController
```

### Creating a Database Model

Models inherit from `skrift.db.base.Base` (provides id, created_at, updated_at):

```python
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from skrift.db.base import Base

class MyModel(Base):
    __tablename__ = "my_models"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
```

### Creating a Service

Services are async functions that handle database operations:

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from skrift.db.models import MyModel

async def get_item_by_id(db_session: AsyncSession, item_id: UUID) -> MyModel | None:
    result = await db_session.execute(select(MyModel).where(MyModel.id == item_id))
    return result.scalar_one_or_none()

async def create_item(db_session: AsyncSession, name: str) -> MyModel:
    item = MyModel(name=name)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item
```

### Using Hooks

The hook system provides WordPress-like extensibility:

```python
from skrift.lib.hooks import hooks, action, filter

# Action: Side effects (no return value needed)
@action("after_page_save", priority=10)
async def notify_on_save(page, is_new: bool):
    print(f"Page saved: {page.title}")

# Filter: Transform values (must return value)
@filter("page_seo_meta", priority=10)
async def customize_meta(meta: dict, page) -> dict:
    meta["author"] = "Custom Author"
    return meta

# Trigger hooks programmatically
await hooks.do_action("my_action", arg1, arg2)
result = await hooks.apply_filters("my_filter", initial_value, arg1)
```

Built-in hooks:
- Actions: `before_page_save`, `after_page_save`, `before_page_delete`, `after_page_delete`
- Filters: `page_seo_meta`, `page_og_meta`, `sitemap_urls`, `sitemap_page`, `robots_txt`, `template_context`

### Using Guards (Authorization)

Protect routes with permission or role guards:

```python
from skrift.auth import auth_guard, Permission, Role

class AdminController(Controller):
    path = "/admin"
    guards = [auth_guard, Permission("manage-pages")]

    @get("/")
    async def admin_dashboard(self) -> TemplateResponse:
        return TemplateResponse("admin/dashboard.html")
```

Combine requirements:
```python
# AND: Both required
guards = [auth_guard, Permission("edit") & Permission("publish")]

# OR: Either sufficient
guards = [auth_guard, Role("admin") | Role("editor")]
```

### Template Resolution

WordPress-style template hierarchy with fallbacks:

```python
from skrift.lib.template import Template

# Tries: page-about.html -> page.html
template = Template("page", "about")

# Tries: post-news-2024.html -> post-news.html -> post.html
template = Template("post", "news", "2024")

return template.render(TEMPLATE_DIR, page=page, extra="context")
```

Templates searched in order:
1. `./templates/` (project directory - user overrides)
2. `skrift/templates/` (package directory - defaults)

## Reference Documentation

For detailed documentation, see:
- [Architecture Details](architecture.md)
- [Code Patterns](patterns.md)
