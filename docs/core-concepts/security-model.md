# Security Model

Skrift's security model is built around one principle: **secure by default, not by configuration**. Every security feature is automatic—you don't need to remember to enable CSRF protection or configure session cookies correctly.

## Session Security

Sessions use encrypted cookies with security flags set automatically:

| Flag | Value | Purpose |
|------|-------|---------|
| `httponly` | `true` | Prevents JavaScript access (XSS mitigation) |
| `secure` | `true` in production | Cookies only sent over HTTPS |
| `samesite` | `lax` | Prevents cross-site request forgery |
| `max_age` | 7 days | Sessions expire automatically |

Session data is encrypted using your `SECRET_KEY`, not just signed. This means session contents cannot be read even if cookies are intercepted.

!!! info "Implementation"
    Session configuration is in `skrift/asgi.py:302-309`. The secret is derived from your `SECRET_KEY` using SHA-256.

## CSRF Protection

Cross-site request forgery protection is built into the OAuth flow automatically.

### How It Works

1. When a user clicks "Login with Google", Skrift generates a cryptographically random state token:

    ```python
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    ```

2. This token is passed to the OAuth provider and stored in the user's session

3. When the provider redirects back, Skrift verifies the state matches:

    ```python
    stored_state = request.session.pop("oauth_state", None)
    if not oauth_state or oauth_state != stored_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    ```

If an attacker tries to forge an OAuth callback, they won't have the matching state token in the victim's session.

!!! info "Implementation"
    CSRF state generation is at `skrift/controllers/auth.py:250-253` and verification at `skrift/controllers/auth.py:290-293`.

## Secret Management

Skrift uses environment variable interpolation to keep secrets out of configuration files:

```yaml
# app.yaml - safe to commit
auth:
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

The `$VAR_NAME` syntax is replaced with the actual environment variable value at runtime. This allows you to:

- Commit `app.yaml` to version control
- Keep actual secrets in environment variables or secret managers
- Use different credentials per environment without file changes

!!! warning "Strict Mode"
    If a referenced environment variable is not set, Skrift raises an error immediately at startup rather than using empty values silently. This prevents accidental deployment with missing credentials.

!!! info "Implementation"
    Environment variable interpolation is in `skrift/config.py:45-69`.

## Development/Production Isolation

Skrift enforces strict separation between development and production configurations.

### Environment-Specific Config Files

| `SKRIFT_ENV` | Config File | Use Case |
|--------------|-------------|----------|
| unset or `production` | `app.yaml` | Production deployment |
| `dev` | `app.dev.yaml` | Local development |
| `staging` | `app.staging.yaml` | Staging environments |

This prevents accidental use of development settings in production.

### Dummy Auth Kill Switch

The dummy authentication provider allows development without OAuth credentials—but it's blocked from production entirely:

```
======================================================================
SECURITY ERROR: Dummy auth provider is configured in production.
Remove 'dummy' from auth.providers in app.yaml.
Server will NOT start.
======================================================================
```

This isn't just a warning that can be ignored. The server process terminates immediately, killing both the worker and the parent uvicorn process to prevent respawning.

!!! info "Implementation"
    The production kill switch is in `skrift/setup/providers.py:179-214`. It uses `os._exit(1)` and signals the parent process to ensure complete shutdown.

## Role-Based Authorization

Skrift includes a flexible authorization system based on roles and permissions.

### Built-in Roles

| Role | Permissions | Description |
|------|-------------|-------------|
| `admin` | `administrator`, `manage-users`, `manage-pages`, `modify-site` | Full system access |
| `editor` | `view-drafts`, `manage-pages` | Content management |
| `author` | `view-drafts` | Content creation |
| `moderator` | `view-drafts` | Content moderation |

### The Administrator Bypass

The `administrator` permission is special—it bypasses all permission checks:

```python
if ADMINISTRATOR_PERMISSION in permissions.permissions:
    return True
```

This means admin users always have access, even to permissions that don't exist yet.

### Using Guards

Protect routes with the `auth_guard` and requirement classes:

```python
from skrift.auth.guards import auth_guard, Permission, Role

@get("/admin/users", guards=[auth_guard, Permission("manage-users")])
async def list_users():
    ...

@get("/editor/drafts", guards=[auth_guard, Role("editor")])
async def view_drafts():
    ...
```

Requirements can be combined with operators:

```python
# User needs BOTH permissions
guards=[auth_guard, Permission("edit") & Permission("publish")]

# User needs EITHER role
guards=[auth_guard, Role("admin") | Role("editor")]
```

!!! info "Implementation"
    Auth guards are defined in `skrift/auth/guards.py`. Role definitions are in `skrift/auth/roles.py`.

See [Protecting Routes](../guides/protecting-routes.md) for a complete guide.

## Security Checklist

Before deploying to production, verify:

- [ ] `SECRET_KEY` is set to a cryptographically random value (not `dev-secret-key`)
- [ ] No `dummy` provider in `app.yaml`
- [ ] `SKRIFT_ENV` is unset or set to `production`
- [ ] Using HTTPS with a valid certificate
- [ ] OAuth redirect URLs point to your production domain
- [ ] Database credentials use environment variables, not hardcoded values

See [Security Checklist](../deployment/security-checklist.md) for the full pre-deployment checklist.
