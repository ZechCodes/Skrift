---
name: skrift
description: "Skrift CMS architecture — app factory, config system, CLI, controllers, middleware, and project conventions."
---

# Skrift CMS Development Guide

Skrift is a lightweight async Python CMS built on Litestar, featuring WordPress-style template resolution, a hook/filter extensibility system, and SQLAlchemy async database access.

## Current Project State

**Configuration:**
!`cat app.yaml 2>/dev/null || echo "No app.yaml found"`

**Controllers:**
!`ls skrift/controllers/*.py 2>/dev/null | head -10`

## Quick Reference

- **Framework**: Litestar (async Python web framework)
- **Database**: SQLAlchemy async with Advanced Alchemy (see `/skrift-db`)
- **Templates**: Jinja2 with WordPress-style hierarchy + themes (see `/skrift-frontend`)
- **Config**: YAML (app.yaml) + environment variables (.env)
- **Auth**: OAuth providers + role-based permissions (see `/skrift-auth`)
- **Forms**: Pydantic-backed with CSRF (see `/skrift-forms`)
- **Events**: Hooks/filters + SSE notifications (see `/skrift-events`)
- **Web Push**: Browser push with SSE fallback (see `/skrift-push`)
- **Multisite**: Multi-subdomain architecture (see `/skrift-multisite`)
- **Observability**: Optional Logfire tracing via `skrift[logfire]`. See `skrift/lib/observability.py`.

## AppDispatcher Pattern

```
                    ┌─────────────────────────────┐
                    │      AppDispatcher          │
                    │  (skrift/asgi.py)           │
                    └─────────────┬───────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                   │
              ▼                   ▼                   ▼
     ┌────────────────┐   ┌────────────┐   ┌────────────────┐
     │   Setup App    │   │  /static   │   │   Main App     │
     │  (/setup/*)    │   │   Files    │   │  (everything)  │
     └────────────────┘   └────────────┘   └────────────────┘
```

- `setup_locked=False`: /setup/* routes active, checks DB for setup completion
- `setup_locked=True`: All traffic goes to main app, /setup/* returns 404
- Main app is lazily created after setup completes (no restart needed)
- Entry point: `skrift.asgi:app` (created by `create_dispatcher()`)

## Configuration System

```
.env (loaded early) → app.yaml (with $VAR interpolation) → Settings (Pydantic)
```

Environment-specific: `app.yaml` (production), `app.dev.yaml` (development), `app.test.yaml` (testing). Set via `SKRIFT_ENV` or overridden with `skrift -f <path>`.

```yaml
db:
  url: $DATABASE_URL
  pool_size: 5
  echo: false
  schema: myschema  # optional; PostgreSQL only

auth:
  redirect_base_url: "https://example.com"
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
      scopes: ["openid", "email", "profile"]

session:
  cookie_domain: null

theme: my-theme

controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController

redis:
  url: $REDIS_URL
  prefix: "myapp"

notifications:
  backend: ""

logfire:
  enabled: true
  service_name: my-site
  console: true

security_headers:
  content_security_policy: "default-src 'self'"

middleware:
  - myapp.middleware:create_logging_middleware
```

### Custom config sections

Extensions add their own typed app.yaml sections without touching core — register at import time, before app creation:

```python
from pydantic import BaseModel
from skrift import register_config_section

class ShopConfig(BaseModel):
    enabled: bool = False
    currency: str = "USD"

register_config_section("shop", ShopConfig)
# app.yaml:  shop: {enabled: true, currency: EUR}
# anywhere:  get_settings().shop.currency  (defaults to ShopConfig() if absent)
```

Built-in sections (db, auth, workers, …) parse through the same registry. Bespoke top-level keys (`storage`, `page_types`, `sites`, scalars) are reserved.

## Public Import Surface

The everyday toolkit is importable from top-level `skrift`:

```python
from skrift import (
    FormModel, Form,                      # forms
    auth_guard, Permission, Role,         # guards
    action, add_filter, do_action, apply_filters, hooks,  # hooks
    flash_success, flash_error,           # flash messages
    notify_user, notify_session, ensure_nid,  # notifications
    get_settings, register_config_section,    # config
    Template, render_markdown,            # rendering
    handler, submit, Job,                 # workers
)
```

Subsystem modules live at guessable top-level paths: `skrift.hooks`, `skrift.forms`, `skrift.notifications`, `skrift.push`, `skrift.flash`, `skrift.template`, `skrift.markdown`, `skrift.seo`, `skrift.storage`. `skrift.lib` is internal — never import from it in downstream code.

## CLI Commands

```bash
skrift serve --reload --port 8080
skrift serve --subdomain blog --port 8081  # serve single subdomain site
skrift -f custom.yaml serve --reload       # use a specific config file
skrift secret --write .env
skrift db upgrade head
skrift db downgrade -1
skrift db revision -m "desc" --autogenerate
```

Global option `-f`/`--config-file` overrides `SKRIFT_ENV`-based config file selection.

## Creating a Controller

```python
from litestar import Controller, get, post
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

class MyController(Controller):
    path = "/my-path"

    @get("/")
    async def list_items(self, db_session: AsyncSession) -> TemplateResponse:
        items = await item_service.list_items(db_session)
        return TemplateResponse("items/list.html", context={"items": items})
```

Register in `app.yaml`:
```yaml
controllers:
  - myapp.controllers:MyController
```

## Middleware

```python
from litestar.middleware import AbstractMiddleware

class LoggingMiddleware(AbstractMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            print(f"Request: {scope['method']} {scope['path']}")
        await self.app(scope, receive, send)

def create_logging_middleware(app):
    return LoggingMiddleware(app=app)
```

Register in `app.yaml`:
```yaml
middleware:
  - myapp.middleware:create_logging_middleware
  - factory: myapp.middleware:create_rate_limit
    kwargs:
      requests_per_minute: 100
```

## Security Headers

`skrift/middleware/security.py` — ASGI middleware injecting CSP, HSTS, X-Frame-Options, etc. CSP nonces are auto-generated per request (see `/skrift-frontend` for usage). HSTS excluded in debug mode.

## Rate Limiting

Per-client sliding-window limits. The backend is chosen once from `redis.url`
(shared across replicas) vs in-memory (process-local) — the built-in
middleware, the failed-auth tracker, and any app code all use the same one, so
setting `redis.url` makes every limiter distributed with no divergence.

Declarative route rules in `app.yaml` (`skrift/config.py:RateLimitConfig`):

```yaml
rate_limit:
  enabled: true
  default: { limit: 120, per: minute }   # unmatched routes
  auth:    { limit: 5,   per: minute }   # /auth/*
  rules:
    - match: { path: /book/inquiry, method: POST }
      key: ip                              # or api_key (bearer / X-API-Key, IP fallback)
      limits:                              # AND-logic; denied requests record nothing
        - { limit: 1, per: minute }
        - { limit: 6, per: hour }
```

`per` is `second|minute|hour|day` or a number of seconds. Most-specific rule
wins (explicit `rules` > legacy `paths` prefixes > `/auth` > `default`); a
matched rule's limits replace the default. Legacy `requests_per_minute` /
`auth_requests_per_minute` / `paths` still work. Over-limit → `429` with a
`Retry-After` for the binding (longest-blocking) window.

Imperative API for dynamic or per-handler limits (`skrift/ratelimit.py`):

```python
from skrift.ratelimit import get_limiter

verdict = await get_limiter().check(
    name="inquiry", key=client_ip,
    limits=[(1, "minute"), (6, "hour")],
)
if not verdict.allowed:
    raise HTTPException(status_code=429, headers={"Retry-After": str(verdict.retry_after)})
```

`check()` denies if any window is exceeded, records nothing on denial, and
reports the binding window's `retry_after`. For the lower-level record/count or
single-window pattern, use `get_limiter().get_counter(window_seconds, name)`.

## Error Handling

Custom exception handlers in `skrift/lib/exceptions.py`. Templates: `error.html`, `error-404.html`, `error-500.html`.

## Testing

```python
from litestar.testing import TestClient

async def test_list_items(client, db_session):
    item = await item_service.create(db_session, name="Test")
    response = client.get("/items")
    assert response.status_code == 200
    assert "Test" in response.text
```

## Key Files

| File | Purpose |
|------|---------|
| `skrift/asgi.py` | AppDispatcher, app creation, middleware loading |
| `skrift/config.py` | Settings management, YAML config loading |
| `skrift/cli.py` | CLI commands (serve, secret, db) |
| `skrift/app_factory.py` | Shared config helpers (sessions, templates, static) |
| `skrift/middleware/` | Security headers, rate limiting, compression |
| `skrift/lib/exceptions.py` | Exception handlers |

## Related Skills

- **`/skrift-db`** — Models, services, migrations, query patterns
- **`/skrift-auth`** — OAuth login, sessions, guards, roles, OAuth2 server
- **`/skrift-events`** — Hooks/filters, SSE notifications, backends
- **`/skrift-push`** — Web Push notifications, service worker
- **`/skrift-forms`** — Form system, CSRF, field customization
- **`/skrift-frontend`** — Templates, themes, static assets, CSP nonces
- **`/skrift-multisite`** — Multi-subdomain architecture
