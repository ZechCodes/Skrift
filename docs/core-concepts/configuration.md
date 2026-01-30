# Configuration

Skrift uses a layered configuration system designed for both convenience and security. Application settings live in `app.yaml`, while secrets stay in environment variables.

## Configuration Sources

| Source | Purpose | Committed to Git? |
|--------|---------|-------------------|
| `app.yaml` | Database, OAuth, controllers | Yes (with `$VAR` references) |
| Environment variables | Secrets, environment selection | No |

## app.yaml

The main configuration file, created automatically by the setup wizard:

```yaml
db:
  url: sqlite+aiosqlite:///./app.db

auth:
  redirect_base_url: http://localhost:8080
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET

controllers:
  - skrift.controllers.auth:AuthController
  - skrift.admin.controller:AdminController
  - skrift.controllers.web:WebController
```

### Secret References

Keep secrets out of your config file using `$VAR_NAME` syntax:

```yaml
db:
  url: $DATABASE_URL

auth:
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

At startup, Skrift replaces `$VAR_NAME` with the actual environment variable value. This allows you to:

- Commit `app.yaml` to version control safely
- Use different credentials per environment
- Rotate secrets without changing config files

!!! warning "Missing Variables"
    If a referenced variable isn't set, Skrift fails immediately at startup with a clear error—not silently using empty values.

### Controllers

Register controllers to add routes:

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController
  - myapp.api:ApiController  # Your custom controller
```

Format: `module.path:ClassName`. Controllers are imported and mounted automatically.

### Middleware

Register custom middleware to process requests:

```yaml
middleware:
  # Simple format - factory function with no arguments
  - myapp.middleware:logging_middleware

  # With configuration - factory with kwargs
  - factory: myapp.middleware:rate_limit_middleware
    kwargs:
      requests_per_minute: 100
```

Format: `module.path:factory_name`. Middleware factories are imported and applied after the built-in session middleware.

See [Custom Middleware](../guides/custom-middleware.md) for a complete guide on writing middleware.

### Database

Configure your database connection:

```yaml
db:
  url: sqlite+aiosqlite:///./app.db  # Development
  # url: $DATABASE_URL                # Production (from env)
  echo: true                          # Log SQL (dev only)
  pool_size: 5                        # Connection pool
  pool_overflow: 10                   # Extra connections allowed
```

### Authentication

Configure OAuth providers and redirect URLs:

```yaml
auth:
  redirect_base_url: http://localhost:8080
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
    github:
      client_id: $GITHUB_CLIENT_ID
      client_secret: $GITHUB_CLIENT_SECRET
```

See [OAuth Providers](../reference/auth-providers.md) for provider-specific setup.

## Environment-Specific Configuration

Different environments need different settings. Skrift uses the `SKRIFT_ENV` variable to select the right config file:

| `SKRIFT_ENV` | Config File | Use Case |
|--------------|-------------|----------|
| unset or `production` | `app.yaml` | Production deployment |
| `dev` | `app.dev.yaml` | Local development |
| `staging` | `app.staging.yaml` | Staging environment |
| `test` | `app.test.yaml` | Automated testing |

### Why This Matters for Security

This separation prevents common security mistakes:

1. **Development credentials don't leak to production** - `app.dev.yaml` might have test OAuth credentials or dummy auth enabled

2. **Production config is the default** - If you forget to set `SKRIFT_ENV`, you get production behavior (stricter, more secure)

3. **Environment-specific features** - Dummy auth only works when `SKRIFT_ENV=dev`

### Example Setup

=== "Production (app.yaml)"

    ```yaml
    db:
      url: $DATABASE_URL
      pool_size: 10

    auth:
      redirect_base_url: https://yourdomain.com
      providers:
        google:
          client_id: $GOOGLE_CLIENT_ID
          client_secret: $GOOGLE_CLIENT_SECRET
    ```

=== "Development (app.dev.yaml)"

    ```yaml
    db:
      url: sqlite+aiosqlite:///./dev.db
      echo: true

    auth:
      redirect_base_url: http://localhost:8080
      providers:
        dummy: {}  # Quick testing
        google:
          client_id: dev-client-id
          client_secret: dev-client-secret
    ```

### Setting the Environment

=== "Shell"

    ```bash
    export SKRIFT_ENV=dev
    python -m skrift
    ```

=== "Docker"

    ```dockerfile
    ENV SKRIFT_ENV=staging
    ```

=== "systemd"

    ```ini
    [Service]
    Environment="SKRIFT_ENV=production"
    ```

## Required Environment Variables

### SECRET_KEY

**Required.** Used for encrypting session cookies.

```bash
export SECRET_KEY=your-secure-random-key
```

Generate a secure key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

!!! danger "Production Security"
    Never use default or weak secret keys in production. A compromised secret key allows session forgery.

### DEBUG

Controls debug mode. Default: `false`

```bash
export DEBUG=true   # Development
export DEBUG=false  # Production
```

When enabled:

- Detailed error pages with stack traces
- Template auto-reload
- SQL query logging possible

## Programmatic Access

Access configuration in your code:

```python
from skrift.config import get_environment, get_config_path, get_settings

# Current environment name
env = get_environment()  # "dev", "staging", "production"

# Path to config file
config_path = get_config_path()  # Path("app.dev.yaml")

# Full settings object
settings = get_settings()
print(settings.debug)  # True/False
print(settings.db.url)  # Database URL
```

## Best Practices

1. **Keep production as the default** - If `SKRIFT_ENV` is unset, production config loads

2. **Use environment variables for all secrets** - Never hardcode credentials

3. **Commit config templates** - `app.yaml` with `$VAR` references is safe to commit

4. **Validate before deploying** - Check that your config file exists and references are set

5. **Use separate OAuth apps per environment** - Don't share Google/GitHub OAuth apps between dev and prod

## See Also

- [Environment Variables](../reference/environment-variables.md) - Complete variable reference
- [OAuth Providers](../reference/auth-providers.md) - Provider setup guides
- [Security Model](security-model.md) - How configuration relates to security
