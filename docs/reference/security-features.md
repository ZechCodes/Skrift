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

## Security Response Headers

### SecurityHeadersConfig

Configuration model in `skrift/config.py`:

```python
class SecurityHeadersConfig(BaseModel):
    enabled: bool = True
    content_security_policy: str | None = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' https:; script-src 'self'; form-action 'self'; base-uri 'self'"
    csp_nonce: bool = True
    strict_transport_security: str | None = "max-age=63072000; includeSubDomains"
    x_content_type_options: str | None = "nosniff"
    x_frame_options: str | None = "DENY"
    referrer_policy: str | None = "strict-origin-when-cross-origin"
    permissions_policy: str | None = "camera=(), microphone=(), geolocation=()"
    cross_origin_opener_policy: str | None = "same-origin"
```

| Field | Type | Default | Effect when `None`/empty |
|-------|------|---------|--------------------------|
| `enabled` | `bool` | `True` | Disables entire middleware |
| `content_security_policy` | `str \| None` | CSP rules | Header omitted |
| `csp_nonce` | `bool` | `True` | Disables per-request nonce generation |
| `strict_transport_security` | `str \| None` | 2-year HSTS | Header omitted |
| `x_content_type_options` | `str \| None` | `nosniff` | Header omitted |
| `x_frame_options` | `str \| None` | `DENY` | Header omitted |
| `referrer_policy` | `str \| None` | `strict-origin-when-cross-origin` | Header omitted |
| `permissions_policy` | `str \| None` | Camera/mic/geo disabled | Header omitted |
| `cross_origin_opener_policy` | `str \| None` | `same-origin` | Header omitted |

### build_headers() Method

```python
def build_headers(self, debug: bool = False) -> list[tuple[bytes, bytes]]:
```

Returns pre-encoded `(name, value)` byte tuples for all enabled headers. Excludes:
- Headers set to `None` or empty string
- HSTS when `debug=True`

### SecurityHeadersMiddleware

ASGI middleware in `skrift/middleware/security.py`:

```python
class SecurityHeadersMiddleware:
    def __init__(self, app, headers: list[tuple[bytes, bytes]],
                 csp_value: str | None = None, csp_nonce: bool = True,
                 debug: bool = False):
        ...
```

Key behaviors:
- Only processes `http` scope (passes through websocket/lifespan)
- Injects headers into `http.response.start` messages
- Does **not** overwrite headers already present in the response (case-insensitive comparison)
- Non-CSP headers are pre-encoded at middleware creation time
- CSP is handled per-request to support nonce injection

### CSP Nonces

When `csp_nonce=True` (the default), the middleware generates a unique nonce for each request and replaces `'unsafe-inline'` in the `style-src` directive with `'nonce-{value}'`.

**Template usage:**

```html
<style nonce="{{ csp_nonce() }}">
    .my-class { color: blue; }
</style>
```

The `csp_nonce()` function is available as a template global. It returns the current request's nonce value (or empty string if nonce is disabled).

The nonce is also stored in `scope["state"]["csp_nonce"]` for access in middleware or handlers.

### Server Header Suppression

The `Server` header is suppressed via `include_server_header=False` in `skrift/cli.py`.

### Example app.yaml Configuration

```yaml
security_headers:
  x_frame_options: "SAMEORIGIN"
  content_security_policy: "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.example.com"
  permissions_policy: "camera=(), microphone=(), geolocation=(), payment=()"
```

## Rate Limiting

### RateLimitConfig

Configuration model in `skrift/config.py`:

```python
class RateLimitConfig(BaseModel):
    enabled: bool = True
    requests_per_minute: int = 60
    auth_requests_per_minute: int = 10
    paths: dict[str, int] = {}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable/disable rate limiting |
| `requests_per_minute` | `int` | `60` | Default limit for all paths |
| `auth_requests_per_minute` | `int` | `10` | Stricter limit for `/auth/*` paths |
| `paths` | `dict[str, int]` | `{}` | Per-path-prefix overrides (e.g., `{"/api": 120}`) |

### How It Works

- **Sliding window**: Uses a 60-second sliding window per IP address
- **Per-IP isolation**: Each client IP has independent rate counters
- **Auth auto-detection**: Paths starting with `/auth` automatically use `auth_requests_per_minute`
- **Longest prefix match**: Custom path overrides use longest-matching prefix
- **Proxy support**: Reads `X-Forwarded-For` header for client IP when behind a reverse proxy
- **429 response**: Returns `429 Too Many Requests` with a `Retry-After` header when the limit is exceeded

### Example app.yaml Configuration

```yaml
rate_limit:
  enabled: true
  requests_per_minute: 120
  auth_requests_per_minute: 5
  paths:
    /api: 200
    /webhooks: 30
```

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
4. Sends `SIGTERM` to parent server process to prevent respawn

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
