# Skrift Architecture

## Application Lifecycle

### AppDispatcher Pattern

Skrift uses a dispatcher architecture that routes between setup and main applications:

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

**Key behaviors:**
- `setup_locked=False`: /setup/* routes active, checks DB for setup completion
- `setup_locked=True`: All traffic goes to main app, /setup/* returns 404
- Main app is lazily created after setup completes (no restart needed)

**Entry point:** `skrift.asgi:app` (created by `create_dispatcher()`)

### Startup Flow

1. `create_dispatcher()` called at module load
2. Checks if setup complete in database
3. If complete + config valid: returns `create_app()` directly
4. Otherwise: returns `AppDispatcher` with lazy main app creation

### Request Flow

```
Request → AppDispatcher → Route Decision:
  │
  ├─ /setup/* or /static/* → Setup App
  │
  ├─ Setup not complete → Check DB:
  │   ├─ Complete → Create main app, lock setup, route to main
  │   └─ Not complete → Redirect to /setup
  │
  └─ Setup locked → Main App (handles everything including 404 for /setup/*)
```

## Configuration System

### Configuration Flow

```
.env (loaded early) → app.yaml (with $VAR interpolation) → Settings (Pydantic)
```

### Environment-Specific Config

| Environment | Config File |
|-------------|-------------|
| production (default) | `app.yaml` |
| development | `app.dev.yaml` |
| testing | `app.test.yaml` |

Set via `SKRIFT_ENV` environment variable.

### app.yaml Structure

```yaml
# Database connection
db:
  url: $DATABASE_URL  # Interpolates from .env
  pool_size: 5
  pool_overflow: 10
  pool_timeout: 30
  echo: false  # SQL logging

# Authentication
auth:
  redirect_base_url: "https://example.com"
  allowed_redirect_domains: []
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
      scopes: ["openid", "email", "profile"]

# Session cookies
session:
  cookie_domain: null  # null = exact host only

# Controllers to load
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController
  - myapp.controllers:CustomController

# Security headers (optional, defaults are secure)
security_headers:
  content_security_policy: "default-src 'self'; script-src 'self' https://cdn.example.com"
  x_frame_options: "SAMEORIGIN"

# Middleware (optional)
middleware:
  - myapp.middleware:create_logging_middleware
  - factory: myapp.middleware:create_rate_limit
    kwargs:
      requests_per_minute: 100
```

### Settings Class (`skrift/config.py`)

```python
class Settings(BaseSettings):
    debug: bool = False
    secret_key: str  # Required - from .env

    db: DatabaseConfig
    auth: AuthConfig
    session: SessionConfig
    security_headers: SecurityHeadersConfig  # Defaults are secure
```

Access settings: `from skrift.config import get_settings`

## Database Layer

### Base Model

All models inherit from `skrift.db.base.Base`:

```python
from advanced_alchemy.base import UUIDAuditBase

class Base(UUIDAuditBase):
    __abstract__ = True
    # Provides: id (UUID), created_at, updated_at
```

### Session Access

Sessions injected via Litestar's SQLAlchemy plugin:

```python
from sqlalchemy.ext.asyncio import AsyncSession

@get("/")
async def handler(self, db_session: AsyncSession) -> dict:
    # db_session is auto-injected
    ...
```

### Core Models

| Model | Table | Purpose |
|-------|-------|---------|
| `User` | `users` | User accounts |
| `OAuthAccount` | `oauth_accounts` | Linked OAuth providers |
| `Role` | `roles` | Permission roles |
| `Page` | `pages` | Content pages |
| `PageRevision` | `page_revisions` | Content history |
| `Setting` | `settings` | Key-value site settings |

### Migrations

Uses Alembic via CLI wrapper:

```bash
skrift db upgrade head                    # Apply all
skrift db downgrade -1                    # Rollback one
skrift db revision -m "add table" --autogenerate  # Generate
```

Migration files in: `skrift/migrations/versions/`

## Authentication & Authorization

### OAuth Flow

```
/auth/{provider}/login → Provider → /auth/{provider}/callback → Session created
```

Providers configured in `app.yaml` under `auth.providers`.

### Session Management

Client-side encrypted cookies (Litestar's CookieBackendConfig):
- 7-day expiry
- HttpOnly, Secure (in production), SameSite=Lax

### Role-Based Authorization

**Built-in Roles:**
- `admin`: Full access (`administrator` permission bypasses all checks)
- `editor`: Can manage pages
- `author`: Can view drafts
- `moderator`: Can moderate content

**Guard System:**

```python
from skrift.auth import auth_guard, Permission, Role

# Basic auth required
guards = [auth_guard]

# With permission
guards = [auth_guard, Permission("manage-pages")]

# With role
guards = [auth_guard, Role("editor")]

# Combinations
guards = [auth_guard, Permission("edit") & Permission("publish")]  # AND
guards = [auth_guard, Role("admin") | Role("editor")]              # OR
```

**Custom Roles:**

```python
from skrift.auth import register_role

register_role(
    "support",
    "view-tickets",
    "respond-tickets",
    display_name="Support Agent",
)
```

## Template System

### Resolution Order

The `Template` class resolves templates from most to least specific:

```python
Template("page", "services", "web")
# Tries: page-services-web.html → page-services.html → page.html
```

### Search Directories

1. `./templates/` (project root - user overrides)
2. `skrift/templates/` (package - defaults)

### Template Globals

Available in all templates:
- `now()` - Current datetime
- `site_name()` - From settings
- `site_tagline()` - From settings
- `site_copyright_holder()` - From settings
- `site_copyright_start_year()` - From settings

### Template Filters

- `markdown` - Render markdown to HTML

## Hook/Filter System

### Concept

WordPress-inspired extensibility:
- **Actions**: Side effects, no return value
- **Filters**: Transform and return values

### Registration Methods

```python
# Decorator (auto-registers on import)
@action("hook_name", priority=10)
async def my_action(arg1, arg2):
    ...

@filter("hook_name", priority=10)
async def my_filter(value, arg1) -> Any:
    return modified_value

# Direct registration
hooks.add_action("hook_name", callback, priority=10)
hooks.add_filter("hook_name", callback, priority=10)
```

### Triggering

```python
# Actions (fire and forget)
await hooks.do_action("hook_name", arg1, arg2)

# Filters (chain transforms)
result = await hooks.apply_filters("hook_name", initial_value, arg1)
```

### Priority

Lower numbers execute first. Default is 10.

### Built-in Hook Points

**Actions:**
- `before_page_save(page, is_new)` - Before saving
- `after_page_save(page, is_new)` - After saving
- `before_page_delete(page)` - Before deletion
- `after_page_delete(page)` - After deletion

**Filters:**
- `page_seo_meta(meta, page)` - Modify SEO metadata
- `page_og_meta(meta, page)` - Modify OpenGraph metadata
- `sitemap_urls(urls)` - Modify sitemap URLs
- `sitemap_page(page_data, page)` - Modify sitemap entry
- `robots_txt(content)` - Modify robots.txt
- `template_context(context)` - Modify template context

## Form System

### Architecture

```
FormModel/BaseModel → Form(model, request) → validate() → hooks → data
      │                      │                    │           │
      │                      │                    │           ├─ form_{name}_validated
      │                      │                    │           └─ form_validated
      │                      │                    │
      │                      │                    ├─ CSRF verify (hmac.compare_digest)
      │                      │                    ├─ Token rotation (single-use)
      │                      │                    ├─ Checkbox injection (bool fields)
      │                      │                    └─ Pydantic validation
      │                      │
      │                      ├─ render() → Template("form", name).try_render()
      │                      ├─ csrf_field() → hidden input
      │                      ├─ fields → dict[str, BoundField]
      │                      └─ errors → dict[str, str]
      │
      └─ Registered in _form_registry (by form_name)
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| `Form` | `skrift/forms/core.py` | Main form handler (CSRF, validation, rendering) |
| `FormModel` | `skrift/forms/model.py` | Base class for form-backed Pydantic models |
| `BoundField` | `skrift/forms/fields.py` | Field bound to form instance (value, error, rendering) |
| `@form()` | `skrift/forms/decorators.py` | Register plain BaseModel as named form |
| `get_form_model()` | `skrift/forms/model.py` | Look up registered form by name |
| `form.html` | `skrift/templates/form.html` | Default form template |

### Registration Mechanism

Forms are registered in a global `_form_registry` dict (in `model.py`):
- `FormModel.__init_subclass__()` auto-registers on class creation
- `@form()` decorator registers explicitly
- `get_form_model(name)` retrieves by name, raises `LookupError` if missing

### CSRF Flow

1. `Form.__init__()` — Creates `secrets.token_urlsafe(32)` in session if absent (key: `_csrf_token`)
2. `csrf_field()` — Renders `<input type="hidden" name="_csrf" value="{token}">`
3. `validate()` — Checks submitted `_csrf` against session token via `hmac.compare_digest()`
4. Token rotation — New token generated after successful check (single-use)

### Template Rendering Hierarchy

```
form.render(submit_label="Submit")
  └─ Template("form", self.name).try_render(template_engine, form=self, submit_label=...)
      ├─ Try: form-{name}.html  (form-specific)
      ├─ Try: form.html          (generic)
      └─ Return None → _render_default() (programmatic fallback)
```

### Hook Integration

Two filter hooks fire after successful validation:

```python
# Form-specific: e.g. "form_contact_validated"
self.data = await hooks.apply_filters(f"form_{self.name}_validated", self.data)

# Global: "form_validated"
self.data = await hooks.apply_filters("form_validated", self.data, self.name)
```

### Jinja2 Global

The `Form` class is registered as a Jinja2 global, making it accessible in templates without passing it through context.

## Security Headers Middleware

`skrift/middleware/security.py` — ASGI middleware that injects security response headers (CSP, HSTS, X-Frame-Options, etc.) into every HTTP response. Configured via `SecurityHeadersConfig` in `skrift/config.py`.

- Pre-encodes headers at creation time (not per-request)
- Does not overwrite headers already set by route handlers
- HSTS excluded in debug mode
- Server header suppressed via `server_header=False` in `skrift/cli.py`

## Static Files

Served from `/static/` with same priority as templates:
1. `./static/` (project root - user overrides)
2. `skrift/static/` (package - defaults)

## Notification System

### Architecture

```
NotificationService (singleton, in-memory)
├── Session queues  →  keyed by session ID (_nid)
├── User queues     →  keyed by user ID
└── Connections     →  asyncio.Queue per active SSE stream
```

**Files:**

| File | Purpose |
|------|---------|
| `skrift/lib/notifications.py` | `NotificationService` singleton, convenience functions |
| `skrift/controllers/notifications.py` | SSE stream + dismiss HTTP endpoints |
| `skrift/static/js/notifications.js` | `SkriftNotifications` client class |
| `skrift/static/css/skrift.css` | `.sk-notification*` toast styles |

### Delivery Scopes

| Function | Stored? | Target | Use case |
|----------|---------|--------|----------|
| `notify_session(nid, type, *, group=None, **payload)` | Yes | Single session | Transient feedback (saves, errors) |
| `notify_user(user_id, type, *, group=None, **payload)` | Yes | All sessions of user | Cross-device (replies, likes) |
| `notify_broadcast(type, *, group=None, **payload)` | No | All connections | Feed updates (new posts) |

Stored notifications replay on reconnect. Broadcast notifications are ephemeral.

**Group key:** When `group` is set, sending a new notification with the same group key automatically dismisses the previous one in the queue and pushes a `"dismissed"` event to active connections. This enables replace-in-place patterns (progress bars, status updates). For broadcasts (ephemeral), the group key flows to the client via `to_dict()` and the client handles replacement.

### SSE Protocol (Three-Phase)

1. **Flush**: Server sends all queued notifications for the session/user
2. **Sync**: Server sends `event: sync` — client reconciles (removes dismissed-elsewhere items)
3. **Live**: Server pushes new notifications as they arrive; 30s keepalive comments prevent proxy timeouts

During the Live phase, group-based replacement sends a `"dismissed"` event for the old notification followed by the new one, so clients see a seamless in-place update.

### Client Behavior

- Auto-connects on page load and `window.focus`, disconnects on `window.blur`
- Reconnects after 5s on error
- Deduplicates via `_displayedIds` Set
- Max visible toasts: 3 (desktop) / 2 (mobile); excess queued
- Dispatches `sk:notification` CustomEvent (cancelable) for every notification
- Only renders `"generic"` type as toast; custom types handled via event listeners
- Global instance: `window.__skriftNotifications`

### Dismiss Flow

1. User clicks dismiss → client adds `.sk-notification-exit` (slide-out animation)
2. Client sends `DELETE /notifications/{id}`
3. Server removes from queues, broadcasts `"dismissed"` event to user's other sessions
4. Other sessions remove the toast via `_removeDismissed()`

**Backend dismiss by group:** The `dismiss` method accepts an optional `group` keyword as an alternative to `notification_id`. Convenience functions `dismiss_session_group(nid, group)` and `dismiss_user_group(user_id, group)` wrap this for common use cases.

### Integration with Hooks

Two hook constants defined in `skrift/lib/hooks.py`:
- `NOTIFICATION_SENT` — action fired after a notification is sent
- `NOTIFICATION_DISMISSED` — action fired after a notification is dismissed

## Error Handling

Custom exception handlers in `skrift/lib/exceptions.py`:
- `HTTPException` → `http_exception_handler`
- `Exception` → `internal_server_error_handler`

Error templates:
- `error.html` - Generic error
- `error-404.html` - Not found
- `error-500.html` - Server error
