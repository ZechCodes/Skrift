# Multi-Subdomain Sites

<span class="skill-badge advanced">:material-star::material-star::material-star: Advanced</span>

Serve multiple subdomain sites from a single Skrift deployment with shared authentication and database.

## Overview

A single Skrift instance can serve `example.com`, `blog.example.com`, and `docs.example.com` — each with its own controllers and theme, all sharing one database and session cookie. Auth and admin live only on the primary domain. No setup wizard, no admin panel, no separate database config needed for subdomains.

## How It Works

```
incoming request
  → AppDispatcher (setup vs main — existing)
    → create_app()
      → SiteDispatcher (only when sites: configured)
        ├── example.com       → primary Litestar app (admin, auth, web)
        ├── blog.example.com  → blog Litestar app (blog controllers)
        └── docs.example.com  → docs Litestar app (docs controllers)
```

The `SiteDispatcher` is an ASGI middleware that reads the `Host` header, extracts the subdomain, and routes to the matching Litestar app. If no subdomain matches, the request goes to the primary app.

Each subdomain app shares:

- **Database** — same SQLAlchemy engine and connection pool
- **Sessions** — same encrypted cookie (scoped to `.example.com`)
- **CSRF** — same config
- **Middleware** — same security headers, rate limiting, and custom middleware

Each subdomain app gets its own:

- **Controllers** — loaded from the site's `controllers` list
- **Theme** — its own template directories and static files
- **Page types** — optional site-specific content types

## Configuration

### app.yaml

```yaml
domain: example.com

controllers:
  - skrift.controllers.auth:AuthController
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController

sites:
  blog:
    subdomain: blog
    controllers:
      - controllers.blog:BlogController
    theme: blog-theme
    page_types:
      - name: post
        plural: posts
        icon: pen-tool

  docs:
    subdomain: docs
    controllers:
      - controllers.docs:DocsController
    theme: docs-theme

session:
  cookie_domain: .example.com

auth:
  redirect_base_url: https://example.com
  allowed_redirect_domains:
    - "*.example.com"
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

### Configuration Reference

| Key | Description | Required |
|-----|-------------|----------|
| `domain` | Primary domain (used for subdomain matching) | Yes (for multisite) |
| `sites` | Map of site name to site config | Yes (for multisite) |
| `sites.<name>.subdomain` | Subdomain prefix (e.g., `blog` for `blog.example.com`) | Yes |
| `sites.<name>.controllers` | Controller import specs | Yes |
| `sites.<name>.theme` | Theme name (falls back to primary theme) | No |
| `sites.<name>.page_types` | Site-specific page types | No |
| `session.cookie_domain` | Must be `.example.com` for cross-subdomain sessions | Yes (for multisite) |
| `auth.allowed_redirect_domains` | Must include `*.example.com` for post-login redirects | Yes (for multisite) |

!!! warning "Three Required Settings"
    Multisite won't work without all three: `domain`, `session.cookie_domain` (with leading dot), and `auth.allowed_redirect_domains` with a wildcard entry. Without these, sessions won't be shared and login redirects will fail.

## Project Structure

```
my-site/
├── app.yaml
├── .env
│
├── controllers/
│   ├── web.py                  # primary site
│   ├── blog.py                 # blog.example.com
│   └── docs.py                 # docs.example.com
│
├── themes/
│   ├── main-theme/             # primary domain theme
│   │   ├── templates/
│   │   └── static/
│   ├── blog-theme/             # blog subdomain theme
│   │   ├── templates/
│   │   └── static/
│   └── docs-theme/             # docs subdomain theme
│       ├── templates/
│       └── static/
│
├── templates/                  # shared template overrides
└── static/                     # shared static assets
```

## Writing Subdomain Controllers

Subdomain controllers are standard Litestar controllers — no special base class or decorator needed:

```python
# controllers/blog.py
from litestar import Controller, get
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession


class BlogController(Controller):
    path = "/"

    @get("/")
    async def index(self, db_session: AsyncSession) -> TemplateResponse:
        # Same database, same models
        from skrift.db.services import page_service

        posts = await page_service.list_pages(
            db_session, page_type="post", published_only=True
        )
        return TemplateResponse("blog/index.html", context={"posts": posts})

    @get("/{slug:str}")
    async def post_detail(
        self, slug: str, db_session: AsyncSession
    ) -> TemplateResponse:
        from skrift.db.services import page_service

        post = await page_service.get_page_by_slug(db_session, slug)
        if not post:
            from litestar.exceptions import NotFoundException
            raise NotFoundException("Post not found")

        return TemplateResponse("blog/post.html", context={"post": post})
```

## Auth Flow from Subdomain

Login happens on the primary domain. Subdomain templates link to it using the `login_url` template global:

```html
{# In a subdomain template #}
<a href="{{ login_url() }}?next={{ request.url }}">Log in</a>
```

The flow:

1. User visits `blog.example.com`, clicks "Log in"
2. Browser navigates to `https://example.com/auth/login?next=https://blog.example.com/`
3. OAuth flow completes on the primary domain
4. Session cookie set with `domain=.example.com` (readable by all subdomains)
5. Redirect back to `https://blog.example.com/` — user is authenticated

No code changes needed. The `allowed_redirect_domains: ["*.example.com"]` config enables safe redirects to subdomains.

## Template Globals

Subdomain apps have the same template globals as the primary app, plus:

| Global | Description |
|--------|-------------|
| `login_url()` | URL to the primary domain login (`https://example.com/auth/login`) |

The `site_name()` global returns the site's config key name (e.g., `"blog"`), and `active_theme()` returns the site's configured theme.

## What Subdomains Don't Have

Subdomain apps intentionally exclude:

- **AdminController** — admin is primary-domain-only
- **AuthController** — login/logout happens on the primary domain
- **NotificationsController** — notifications are a primary domain feature
- **Setup routes** — setup is primary-domain-only

Requests to `/admin` on a subdomain return 404.

## Backward Compatibility

No `sites:` key in `app.yaml` = existing single-site behavior, completely unchanged. The `SiteDispatcher` is only created when both `domain` and `sites` are present.

## Development

### Option 1: `--subdomain` flag (recommended)

Use the `--subdomain` CLI option to serve a single subdomain site on its own port — no `/etc/hosts` changes needed:

```bash
# Primary site on default port
skrift serve --reload --port 8080

# Blog subdomain on a separate port
skrift serve --reload --subdomain blog --port 8081

# Docs subdomain on another port
skrift serve --reload --subdomain docs --port 8082
```

All HTTP requests are routed to the specified subdomain's app regardless of the `Host` header, so `localhost:8081` behaves exactly like `blog.example.com` would in production.

### Option 2: Local DNS

Alternatively, use `/etc/hosts` or a local DNS tool to point subdomains to localhost:

```
# /etc/hosts
127.0.0.1 example.local
127.0.0.1 blog.example.local
127.0.0.1 docs.example.local
```

Then in `app.dev.yaml`:

```yaml
domain: example.local

sites:
  blog:
    subdomain: blog
    controllers:
      - controllers.blog:BlogController
    theme: blog-theme

session:
  cookie_domain: .example.local
```

## See Also

- [Configuration](../core-concepts/configuration.md) — Full config reference
- [Custom Controllers](custom-controllers.md) — Writing controllers
- [Custom Templates](custom-templates.md) — Template hierarchy
- [Protecting Routes](protecting-routes.md) — Auth guards (work the same on subdomains)
