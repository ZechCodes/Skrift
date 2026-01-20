# Base Site Documentation

A Litestar-powered web application with Google OAuth authentication and WordPress-like template resolution.

For styling documentation, see: [CSS Framework](css-framework.md)

## Table of Contents

- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Authentication](#authentication)
- [Template System](#template-system)
- [Controllers & Routes](#controllers--routes)
- [Database](#database)
- [Error Handling](#error-handling)
- [Production Deployment](#production-deployment)

## Project Structure

```
base-site/
├── app/
│   ├── __init__.py
│   ├── asgi.py              # Application factory and Litestar setup
│   ├── config.py            # Settings and environment configuration
│   ├── controllers/
│   │   ├── auth.py          # Google OAuth authentication routes
│   │   └── web.py           # Main web routes (pages, posts)
│   ├── db/
│   │   ├── base.py          # SQLAlchemy base model
│   │   └── models/
│   │       └── user.py      # User model
│   └── lib/
│       ├── exceptions.py    # Custom exception handlers
│       └── template.py      # WordPress-like template resolver
├── docs/
│   ├── README.md            # This file
│   └── css-framework.md     # CSS styling documentation
├── static/
│   └── css/
│       └── style.css        # Application styles
├── templates/
│   ├── base.html            # Base layout template
│   ├── index.html           # Home page
│   ├── page.html            # Default page template
│   ├── post.html            # Default post template
│   ├── error.html           # Default error template
│   ├── error-404.html       # 404 error template
│   ├── error-500.html       # 500 error template
│   └── auth/
│       └── login.html       # Login page
├── .env                     # Environment variables (not in git)
├── .env.example             # Environment template
├── app.yaml                 # Controller configuration
├── main.py                  # Development server entry point
└── pyproject.toml           # Project dependencies
```

## Quick Start

### Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) package manager

### Setup

1. Clone the repository and navigate to the project directory.

2. Copy the environment template:
   ```bash
   cp .env.example .env
   ```

3. Edit `.env` with your configuration (see [Configuration](#configuration)).

4. Install dependencies and run:
   ```bash
   uv sync
   uv run python main.py
   ```

5. Open http://localhost:8080 in your browser.

## Configuration

Configuration is managed via environment variables, loaded from `.env` using Pydantic Settings.

### Settings Class

The `Settings` class in `app/config.py` defines all configuration options:

```python
class Settings(BaseSettings):
    # Application
    debug: bool = False
    secret_key: str

    # Database
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # Google OAuth
    google_client_id: str
    google_client_secret: str
    oauth_redirect_base_url: str = "http://localhost:8000"
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes | - | Secret key for session encryption |
| `DEBUG` | No | `false` | Enable debug mode |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./app.db` | Database connection string |
| `GOOGLE_CLIENT_ID` | Yes | - | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | - | Google OAuth client secret |
| `OAUTH_REDIRECT_BASE_URL` | No | `http://localhost:8000` | Base URL for OAuth callbacks |

### Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create a new project or select an existing one
3. Enable the Google+ API
4. Create OAuth 2.0 credentials:
   - Application type: Web application
   - Authorized redirect URIs: `http://localhost:8080/auth/google/callback`
5. Copy the Client ID and Client Secret to your `.env` file

## Authentication

### Google OAuth Flow

1. User clicks "Login" and is redirected to `/auth/google/login`
2. A CSRF state token is generated and stored in the session
3. User is redirected to Google's consent screen
4. After consent, Google redirects to `/auth/google/callback` with an authorization code
5. The callback handler:
   - Verifies the CSRF state token
   - Exchanges the code for access tokens
   - Fetches user info from Google
   - Creates or updates the user record
   - Sets the user ID in the session
6. User is redirected to the home page

### Session Management

Sessions use encrypted client-side cookies:
- **Max age**: 7 days
- **Security**: HttpOnly, SameSite=Lax, Secure (in production)
- **Storage**: User ID stored as `user_id` in session

### User Model Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key (auto-generated) |
| `oauth_provider` | String | OAuth provider name ("google") |
| `oauth_id` | String | Unique ID from OAuth provider |
| `email` | String | User's email address |
| `name` | String | User's display name |
| `picture_url` | String | Profile picture URL |
| `is_active` | Boolean | Account active status |
| `last_login_at` | DateTime | Last login timestamp |
| `created_at` | DateTime | Record creation timestamp |
| `updated_at` | DateTime | Last update timestamp |

## Template System

The application uses a WordPress-like template resolution system that selects templates based on specificity.

### How Template Resolution Works

The `Template` class in `app/lib/template.py` resolves templates from most specific to least specific:

```python
Template("post", "about")
# Tries: post-about.html → post.html

Template("page", "services", "web")
# Tries: page-services-web.html → page-services.html → page.html
```

### Using Template with Context

The `Template` class accepts a `context` parameter and provides a `render()` method for convenient usage:

```python
# Create template with initial context
template = Template("post", slug, context={"slug": slug})

# Render with additional context (merged with initial context)
return template.render(TEMPLATE_DIR, flash=flash, user=user)
```

**Context merging**: When `render()` is called, the initial context from the constructor is merged with any additional keyword arguments. Extra context takes precedence for duplicate keys.

You can also use `resolve()` directly if you need more control:

```python
template = Template("post", slug)
template_name = template.resolve(TEMPLATE_DIR)  # Returns "post-slug.html" or "post.html"
return TemplateResponse(template_name, context={...})
```

### Template Hierarchy Examples

**Posts** (`/post/{slug}`):
- `/post/about` → `post-about.html` → `post.html`
- `/post/contact` → `post-contact.html` → `post.html`

**Pages** (`/page/{path}`):
- `/page/services` → `page-services.html` → `page.html`
- `/page/services/web` → `page-services-web.html` → `page-services.html` → `page.html`
- `/page/about/team/leadership` → `page-about-team-leadership.html` → `page-about-team.html` → `page-about.html` → `page.html`

### Template Context Variables

All templates receive these context variables:

| Variable | Type | Description |
|----------|------|-------------|
| `user` | User \| None | Current logged-in user or None |
| `flash` | str \| None | Flash message from session |
| `now` | callable | Function returning current datetime |

Route-specific variables:
- **Posts**: `slug` (the post slug)
- **Pages**: `path` (full path), `slugs` (list of path segments)

### Base Template Blocks

The `base.html` template defines these blocks for child templates:

| Block | Purpose |
|-------|---------|
| `title` | Page title (default: "Base Site") |
| `head` | Additional `<head>` content |
| `main_class` | Additional classes for `<main>` |
| `content` | Main page content |
| `scripts` | JavaScript before `</body>` |

Example usage:
```html
{% extends "base.html" %}

{% block title %}My Page{% endblock %}

{% block content %}
<h1>Welcome</h1>
<p>Page content here.</p>
{% endblock %}
```

## Controllers & Routes

### Route Table

| Method | Path | Controller | Handler | Description |
|--------|------|------------|---------|-------------|
| GET | `/` | WebController | `index` | Home page |
| GET | `/post/{slug}` | WebController | `post` | Post page with template resolution |
| GET | `/page/{path:path}` | WebController | `page` | Page with nested path support |
| GET | `/auth/login` | AuthController | `login_page` | Login page |
| GET | `/auth/logout` | AuthController | `logout` | Clear session and redirect |
| GET | `/auth/google/login` | AuthController | `google_login` | Initiate Google OAuth |
| GET | `/auth/google/callback` | AuthController | `google_callback` | Handle OAuth callback |
| GET | `/static/*` | Static Files Router | - | Serve static assets |

### Controller Configuration

Controllers are loaded dynamically from `app.yaml` at startup. This allows you to add or remove controllers without modifying the application code.

**app.yaml format:**
```yaml
controllers:
  - app.controllers.auth:AuthController
  - app.controllers.web:WebController
```

Each controller entry uses the format `module.path:ControllerClass`, where the module path is relative to the current working directory.

### Adding New Routes

1. **Create a controller** in `app/controllers/` (or any location within your project):

```python
# app/controllers/my_controller.py
from litestar import Controller, get
from litestar.response import Template as TemplateResponse
from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession

class MyController(Controller):
    path = "/my-path"

    @get("/")
    async def my_handler(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        return TemplateResponse("my-template.html", context={})
```

2. **Register the controller** in `app.yaml`:

```yaml
controllers:
  - app.controllers.auth:AuthController
  - app.controllers.web:WebController
  - app.controllers.my_controller:MyController  # Add your controller
```

3. **Restart the application** for the changes to take effect.

The application will automatically import and register all controllers listed in `app.yaml`.

## Database

### SQLAlchemy Async Setup

The application uses SQLAlchemy with async support via Advanced Alchemy:

- **Session injection**: `AsyncSession` is automatically injected into route handlers
- **Auto-create tables**: Tables are created on startup (`create_all=True`)
- **Session config**: `expire_on_commit=False` for better async compatibility

### Base Model

All models inherit from `Base` (`app/db/base.py`), which provides:

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key (auto-generated) |
| `created_at` | DateTime | Automatic creation timestamp |
| `updated_at` | DateTime | Automatic update timestamp |

### Supported Databases

**SQLite** (default, development):
```
DATABASE_URL=sqlite+aiosqlite:///./app.db
```

**PostgreSQL** (production):
```
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
```

## Error Handling

### Content Negotiation

Error handlers automatically detect the client type:
- **Browsers** (Accept: text/html): Receive HTML error pages
- **API clients**: Receive JSON responses

### Custom Error Templates

Create templates following the naming pattern `error-{status_code}.html`:

- `error-404.html` - Not Found
- `error-500.html` - Internal Server Error
- `error.html` - Default fallback for all errors

### Template Fallback Hierarchy

1. `error-{status_code}.html` (e.g., `error-404.html`)
2. `error.html` (generic fallback)

Error templates receive these context variables:

| Variable | Type | Description |
|----------|------|-------------|
| `status_code` | int | HTTP status code |
| `message` | str | Error message/detail |
| `user` | None | Always None in error context |

## Production Deployment

### Environment Changes

Update your `.env` for production:

```bash
DEBUG=false
SECRET_KEY=<strong-random-key>
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
OAUTH_REDIRECT_BASE_URL=https://yourdomain.com
```

Generate a secure secret key:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Running with Gunicorn

Install gunicorn with uvicorn workers:
```bash
uv add gunicorn
```

Run the application:
```bash
gunicorn app.asgi:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

### Database Considerations

For production with PostgreSQL:

1. Ensure `asyncpg` is installed (included in dependencies)
2. Set up your PostgreSQL database
3. Update `DATABASE_URL` in your environment
4. Consider using database migrations (e.g., Alembic) for schema changes

The application will automatically create tables on first run, but for production deployments, you may want to manage schema migrations separately.
