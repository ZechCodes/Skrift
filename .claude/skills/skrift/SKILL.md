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
