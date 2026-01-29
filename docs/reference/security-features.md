# Security Features Reference

Technical reference for Skrift's security implementations. For conceptual overview, see [Security Model](../core-concepts/security-model.md).

## Session Security

### Cookie Configuration

Sessions are configured in `skrift/asgi.py:302-309`:

```python
session_config = CookieBackendConfig(
    secret=session_secret,
    max_age=60 * 60 * 24 * 7,  # 7 days
    httponly=True,
    secure=not settings.debug,
    samesite="lax",
)
```

| Parameter | Value | Effect |
|-----------|-------|--------|
| `secret` | SHA-256 of `SECRET_KEY` | Encrypts session data |
| `max_age` | 604800 (7 days) | Session expiration |
| `httponly` | `True` | No JavaScript access |
| `secure` | `True` in production | HTTPS only |
| `samesite` | `lax` | Cross-site request protection |

### Secret Derivation

The session secret is derived from your `SECRET_KEY`:

```python
session_secret = hashlib.sha256(settings.secret_key.encode()).digest()
```

This produces a 32-byte key suitable for encryption.

## CSRF Protection

### State Token Generation

OAuth state tokens are generated in `skrift/controllers/auth.py:250-253`:

```python
state = secrets.token_urlsafe(32)
request.session["oauth_state"] = state
request.session["oauth_provider"] = provider
```

`secrets.token_urlsafe(32)` produces 256 bits of cryptographic randomness, URL-safe encoded.

### State Token Verification

Verification occurs in `skrift/controllers/auth.py:290-293`:

```python
stored_state = request.session.pop("oauth_state", None)
if not oauth_state or oauth_state != stored_state:
    raise HTTPException(status_code=400, detail="Invalid OAuth state")
```

The state is popped (removed) from the session immediately, preventing replay attacks.

### PKCE for Twitter

Twitter/X uses PKCE (Proof Key for Code Exchange) with S256:

```python
code_verifier = secrets.token_urlsafe(64)[:128]
code_challenge = base64.urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()
).decode().rstrip("=")
```

## Environment Variable Interpolation

### Implementation

Interpolation is in `skrift/config.py:45-69`:

```python
def interpolate_env_vars(value, strict: bool = True):
    if isinstance(value, str):
        def replace(match):
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                if strict:
                    raise ValueError(f"Environment variable ${var} not set")
                return match.group(0)
            return val
        return ENV_VAR_PATTERN.sub(replace, value)
    elif isinstance(value, dict):
        return {k: interpolate_env_vars(v, strict) for k, v in value.items()}
    elif isinstance(value, list):
        return [interpolate_env_vars(item, strict) for item in value]
    return value
```

### Pattern

The regex pattern matches `$VAR_NAME`:

```python
ENV_VAR_PATTERN = re.compile(r'\$([A-Z_][A-Z0-9_]*)')
```

Only uppercase letters, numbers, and underscores are valid.

### Strict Mode

When `strict=True` (default), missing variables raise `ValueError`. This prevents silent failures with empty credentials.

## Production Safety

### Dummy Auth Kill Switch

Implementation in `skrift/setup/providers.py:179-214`:

```python
def validate_no_dummy_auth_in_production() -> None:
    if get_environment() != "production":
        return

    config = load_raw_app_config()
    providers = config.get("auth", {}).get("providers", {})

    if DUMMY_PROVIDER_KEY in providers:
        sys.stderr.write(
            "\nSECURITY ERROR: Dummy auth provider is configured in production.\n"
            "Remove 'dummy' from auth.providers in app.yaml.\n"
            "Server will NOT start.\n"
        )
        # Kill parent process to prevent respawning
        os.kill(os.getppid(), signal.SIGTERM)
        os._exit(1)
```

Key aspects:

1. Checks `SKRIFT_ENV` - only blocks in production
2. Reads raw config before interpolation to catch `dummy: {}`
3. Uses `os._exit(1)` for immediate termination
4. Sends `SIGTERM` to parent (uvicorn) to prevent respawn

## Auth Guards

### AuthRequirement Base Class

From `skrift/auth/guards.py`:

```python
class AuthRequirement(ABC):
    @abstractmethod
    async def check(self, permissions: "UserPermissions") -> bool:
        ...

    def __or__(self, other: "AuthRequirement") -> "OrRequirement":
        return OrRequirement(self, other)

    def __and__(self, other: "AuthRequirement") -> "AndRequirement":
        return AndRequirement(self, other)
```

### Permission Check

```python
class Permission(AuthRequirement):
    async def check(self, permissions: "UserPermissions") -> bool:
        if ADMINISTRATOR_PERMISSION in permissions.permissions:
            return True
        return self.permission in permissions.permissions
```

The `administrator` permission always grants access.

### Role Check

```python
class Role(AuthRequirement):
    async def check(self, permissions: "UserPermissions") -> bool:
        if ADMINISTRATOR_PERMISSION in permissions.permissions:
            return True
        return self.role in permissions.roles
```

### auth_guard Function

```python
async def auth_guard(connection: ASGIConnection, route_handler: BaseRouteHandler) -> None:
    user_id = connection.session.get("user_id")

    if not user_id:
        raise NotAuthorizedException("Authentication required")

    guards = route_handler.guards or []
    auth_requirements = [g for g in guards if isinstance(g, AuthRequirement)]

    if not auth_requirements:
        return  # Just needs login

    async with session_maker() as session:
        permissions = await get_user_permissions(session, user_id)

    for requirement in auth_requirements:
        if not await requirement.check(permissions):
            raise NotAuthorizedException("Insufficient permissions")
```

## Role Definitions

### Built-in Roles

From `skrift/auth/roles.py`:

```python
ADMIN = create_role(
    "admin",
    "administrator", "manage-users", "manage-pages", "modify-site",
    display_name="Administrator",
)

EDITOR = create_role(
    "editor",
    "view-drafts", "manage-pages",
    display_name="Editor",
)

AUTHOR = create_role(
    "author",
    "view-drafts",
    display_name="Author",
)

MODERATOR = create_role(
    "moderator",
    "view-drafts",
    display_name="Moderator",
)
```

### Custom Roles

Register custom roles:

```python
from skrift.auth.roles import register_role, create_role

CUSTOM_ROLE = create_role(
    "custom",
    "custom-permission-1",
    "custom-permission-2",
    display_name="Custom Role",
)

register_role(CUSTOM_ROLE)
```

## See Also

- [Security Model](../core-concepts/security-model.md) - Conceptual overview
- [Protecting Routes](../guides/protecting-routes.md) - Practical guide
- [Security Checklist](../deployment/security-checklist.md) - Deployment verification
