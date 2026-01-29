# Development

This section covers development-specific features in Skrift. These features make local development faster but are intentionally blocked from production.

## Development Environment

Set up your development environment:

```bash
# Set environment to dev
export SKRIFT_ENV=dev

# Use a simple dev secret (never use in production!)
export SECRET_KEY=dev-secret-key-change-in-production

# Enable debug mode
export DEBUG=true

# Run Skrift
python -m skrift
```

With `SKRIFT_ENV=dev`, Skrift loads `app.dev.yaml` instead of `app.yaml`.

## Development vs Production

| Feature | Development | Production |
|---------|-------------|------------|
| Config file | `app.dev.yaml` | `app.yaml` |
| Debug mode | Enabled | Disabled |
| Secure cookies | Optional | Required |
| Dummy auth | Allowed | Blocked |
| SQLite | Recommended | Not recommended |
| Hot reload | Yes | No |

## Development-Only Features

### Dummy Authentication

The dummy auth provider lets you test authentication flows without setting up OAuth. See [Dummy Authentication](dummy-auth.md) for details.

### Debug Mode

When `DEBUG=true`:

- Detailed error pages with stack traces
- Template auto-reload on changes
- SQL query logging (with `db.echo: true`)
- Litestar debug mode enabled

!!! warning
    Never enable debug mode in production. It exposes internal application details.

### SQLite Database

SQLite is perfect for development:

```yaml
# app.dev.yaml
db:
  url: sqlite+aiosqlite:///./dev.db
  echo: true  # Log SQL queries
```

No server setup required. Database is a single file you can delete to start fresh.

## Example Development Config

```yaml
# app.dev.yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController

db:
  url: sqlite+aiosqlite:///./dev.db
  echo: true

auth:
  redirect_base_url: http://localhost:8080
  providers:
    dummy: {}  # Quick testing without OAuth
    google:    # Or use real OAuth if you prefer
      client_id: your-dev-client-id
      client_secret: your-dev-client-secret
```

## What's in This Section

<div class="grid cards" markdown>

-   [**Dummy Authentication**](dummy-auth.md)

    Test auth flows without OAuth credentials.

</div>
