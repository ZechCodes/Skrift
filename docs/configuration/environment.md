# Environment Variables

Reference for environment variables used by Skrift.

## Overview

Skrift uses environment variables for secrets that shouldn't be stored in configuration files. Application settings like database and OAuth go in `app.yaml`, which can reference environment variables using `$VAR_NAME` syntax.

## Required Variables

### SECRET_KEY

**Required.** Used for encrypting session cookies.

```bash
export SECRET_KEY=your-secure-secret-key
```

Generate a secure key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

!!! warning "Production Security"
    Never commit your secret key to version control. Set it as an environment variable in your deployment environment.

### DEBUG

Controls debug mode. Default: `false`

```bash
export DEBUG=true   # Development
export DEBUG=false  # Production
```

When `true`:

- Enables Litestar debug mode
- Enables hot reload for templates
- Shows detailed error pages

## Referencing in app.yaml

Keep secrets out of your config file by referencing environment variables:

```yaml
db:
  url: $DATABASE_URL

auth:
  redirect_base_url: https://yourdomain.com
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

This allows you to commit `app.yaml` to version control while keeping credentials secure.

## Common Variables

These are commonly referenced from `app.yaml`:

| Variable | Used In | Description |
|----------|---------|-------------|
| `SKRIFT_ENV` | Config loading | Selects environment-specific config file |
| `DATABASE_URL` | `db.url` | Database connection string |
| `GOOGLE_CLIENT_ID` | `auth.providers.google.client_id` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | `auth.providers.google.client_secret` | Google OAuth secret |
| `GITHUB_CLIENT_ID` | `auth.providers.github.client_id` | GitHub OAuth client ID |
| `GITHUB_CLIENT_SECRET` | `auth.providers.github.client_secret` | GitHub OAuth secret |

## SKRIFT_ENV

Controls which configuration file Skrift loads.

| SKRIFT_ENV Value | Config File |
|------------------|-------------|
| (unset or "production") | `app.yaml` |
| `dev` | `app.dev.yaml` |
| `staging` | `app.staging.yaml` |

See [Environment-Specific Configuration](environments.md) for details.

## Database URL Format

Skrift uses async database drivers:

=== "SQLite"

    ```bash
    export DATABASE_URL=sqlite+aiosqlite:///./app.db
    ```

=== "PostgreSQL"

    ```bash
    export DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
    ```

## Example Setup

### Development

Set environment variables for development:

```bash
export SECRET_KEY=dev-secret-key-change-in-production
export DEBUG=true
```

With `app.yaml`:

```yaml
db:
  url: sqlite+aiosqlite:///./app.db

auth:
  redirect_base_url: http://localhost:8080
  providers:
    google:
      client_id: your-dev-client-id
      client_secret: your-dev-client-secret
```

### Production

Set environment variables securely (e.g., in systemd, Docker, or your hosting platform):

```bash
export SECRET_KEY=your-production-secret-key
export DATABASE_URL=postgresql+asyncpg://user:password@db.example.com:5432/skrift
export GOOGLE_CLIENT_ID=your-prod-client-id
export GOOGLE_CLIENT_SECRET=your-prod-client-secret
```

With `app.yaml`:

```yaml
db:
  url: $DATABASE_URL

auth:
  redirect_base_url: https://yourdomain.com
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

## Loading Priority

1. Default values in application code
2. System environment variables

## Validation

Missing required variables cause an immediate startup error:

```
pydantic_settings.ValidationError: 1 validation error for Settings
secret_key
  Field required [type=missing, input_value={...}]
```

## See Also

- [OAuth Providers](auth-providers.md) - OAuth configuration in app.yaml
- [Production Deployment](../deployment/production.md) - Production setup
