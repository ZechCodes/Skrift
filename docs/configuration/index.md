# Configuration

Skrift uses `app.yaml` for application settings. Secrets are stored as environment variables, which can be referenced in `app.yaml` using `$VAR_NAME` syntax.

## Configuration

| Source | Purpose |
|--------|---------|
| `app.yaml` | Database, OAuth providers, controllers |
| Environment variables | Secrets (SECRET_KEY, credentials referenced with `$VAR_NAME`) |

## app.yaml

The main configuration file for your Skrift site. Created automatically by the setup wizard.

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

### Environment Variable References

Reference environment variables in `app.yaml` using the `$VAR_NAME` syntax:

```yaml
db:
  url: $DATABASE_URL

auth:
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

This keeps secrets out of your config file while allowing you to commit `app.yaml` to version control.

## Environment Variables

Set these in your deployment environment:

```bash
export SECRET_KEY=your-secure-secret-key
```

See [Environment Variables](environment.md) for details.

## Configuration Topics

<div class="grid cards" markdown>

-   :material-key:{ .lg .middle } **Environment Variables**

    ---

    Reference for `.env` settings.

    [:octicons-arrow-right-24: Environment Variables](environment.md)

-   :material-shield-account:{ .lg .middle } **OAuth Providers**

    ---

    Set up authentication providers in `app.yaml`.

    [:octicons-arrow-right-24: OAuth Providers](auth-providers.md)

</div>

## Controller Configuration

Controllers are registered in `app.yaml`:

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController
  - myapp.controllers.api:ApiController  # Your custom controller
```

Each entry follows the format `module.path:ClassName`.
