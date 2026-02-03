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

## Static Files

Served from `/static/` with same priority as templates:
1. `./static/` (project root - user overrides)
2. `skrift/static/` (package - defaults)

## Error Handling

Custom exception handlers in `skrift/lib/exceptions.py`:
- `HTTPException` → `http_exception_handler`
- `Exception` → `internal_server_error_handler`

Error templates:
- `error.html` - Generic error
- `error-404.html` - Not found
- `error-500.html` - Server error
