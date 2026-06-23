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

Rate limiting supports **declarative multi-window route rules** in config and an
**imperative limiter API** for dynamic or per-handler limits. Both run through a
single backend-aware limiter, so the choice of backend is made in exactly one
place.

### Backend selection

The limiter uses **Redis** when `redis.url` is set (counts are shared across all
replicas) and an **in-memory** counter otherwise (process-local). This applies
to every limiter — the built-in middleware, the failed-auth tracker, and any app
code that calls `get_limiter()` — so setting `redis.url` makes them all
distributed at once, with no silent divergence.

### How It Works

- **Sliding window**: per-key sliding windows; multiple windows can apply to one route
- **Multi-window AND-logic**: a request is denied if *any* window is exceeded
- **All-or-nothing recording**: a denied request records nothing — being blocked by one window never consumes a slot in another
- **Per-client isolation**: each client (IP or API key) has independent counters
- **Auth auto-detection**: paths starting with `/auth` use the stricter auth window
- **Most-specific rule wins**: explicit rules → legacy path prefixes (longest wins) → `/auth` → default; a matched rule's limits *replace* the default rather than stacking
- **Proxy support**: uses the resolved client IP (`X-Forwarded-For` honored only behind trusted proxies)
- **429 response**: returns `429 Too Many Requests` with a `Retry-After` header reflecting the binding (longest-blocking) window

### Declarative configuration

```yaml
rate_limit:
  enabled: true
  default: { limit: 120, per: minute }   # applies to unmatched routes
  auth:    { limit: 5,   per: minute }   # applies to /auth/*
  rules:
    # Anonymous lead-capture endpoint: 1/min AND 6/hr per client IP.
    - match: { path: /book/inquiry, method: POST }
      key: ip
      limits:
        - { limit: 1, per: minute }
        - { limit: 6, per: hour }
    # Prefix match (all methods) with an explicit per-second cap.
    - match: { path: /api/, prefix: true }
      key: api_key                        # bearer token / X-API-Key, falls back to IP
      limits:
        - { limit: 20, per: second }
```

`per` accepts `second`, `minute`, `hour`, `day`, or an explicit number of
seconds. `key` is `ip` (default) or `api_key`. `match` selects requests by exact
`path` (or `prefix: true`) and optional `method`.

### Configuration fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable/disable rate limiting |
| `default` | window | — | Default window for unmatched routes |
| `auth` | window | — | Window for `/auth/*` paths |
| `rules` | list | `[]` | Declarative multi-window route rules |
| `requests_per_minute` | `int` | `60` | **Legacy** default (used when `default` is unset) |
| `auth_requests_per_minute` | `int` | `10` | **Legacy** auth limit (used when `auth` is unset) |
| `paths` | `dict[str, int]` | `{}` | **Legacy** per-prefix per-minute overrides |

### Migration from the legacy keys

The legacy `requests_per_minute`, `auth_requests_per_minute`, and `paths` keys
still work unchanged — they are treated as single per-minute windows. To adopt
multiple windows or non-minute periods, move them to `default`/`auth`/`rules`:

```yaml
# Before
rate_limit:
  requests_per_minute: 120
  auth_requests_per_minute: 5
  paths:
    /api: 200

# After
rate_limit:
  default: { limit: 120, per: minute }
  auth:    { limit: 5,   per: minute }
  rules:
    - match: { path: /api, prefix: true }
      limits: [ { limit: 200, per: minute } ]
```

### Imperative limiter API

For limits that can't be expressed as static route config (per-user actions,
limits enforced inside a handler or guard, background jobs), call the shared
limiter directly. It uses the same backend as everything else:

```python
from skrift.ratelimit import get_limiter
from litestar.exceptions import HTTPException

verdict = await get_limiter().check(
    name="inquiry",                       # namespace for the counter
    key=client_ip,                        # caller identity
    limits=[(1, "minute"), (6, "hour")],  # one or more (limit, window) pairs
)
if not verdict.allowed:
    raise HTTPException(
        status_code=429,
        headers={"Retry-After": str(verdict.retry_after)},
    )
```

`check()` evaluates every window, denies if any is exceeded, records nothing on
denial, and reports `retry_after` (seconds) for the binding window. For the
lower-level `record`/`count` or single-window `check_and_record` pattern (e.g. a
failed-attempt tracker), use `get_limiter().get_counter(window_seconds, name)`.

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

## OAuth2 Token Security

### Token JTI (JWT ID)

Every token created by `create_signed_token()` includes a unique `jti` field (UUID hex, 32 characters). This enables token revocation by tracking revoked JTIs in the `revoked_tokens` database table.

```python
# In skrift/auth/tokens.py:
payload = {**payload, "jti": uuid.uuid4().hex, "exp": int(time.time()) + expires_in}
```

### Token Revocation (RFC 7009)

`POST /oauth/revoke` accepts a `token` parameter and always returns HTTP 200, per RFC 7009. If the token is valid and has a `jti`, the JTI is recorded in `revoked_tokens` with the token's expiration time.

```python
# skrift/controllers/oauth2.py
async def revoke(self, request, db_session):
    # Parse token, extract jti, record in revoked_tokens
    # Always returns 200 (even for invalid tokens)
```

Revoked tokens are checked by `verify_oauth_token()`, which wraps `verify_signed_token()` with a database lookup:

```python
async def verify_oauth_token(token, secret, db_session):
    payload = verify_signed_token(token, secret)  # Crypto check
    if payload and payload.get("jti"):
        if await oauth2_service.is_token_revoked(db_session, payload["jti"]):
            return None  # Token was revoked
    return payload
```

### Refresh Token Rotation

When a refresh token is used at `/oauth/token` (`grant_type=refresh_token`), the old refresh token is automatically revoked before issuing new tokens. This limits the window for token reuse attacks.

### Token Introspection (RFC 7662)

`POST /oauth/introspect` requires client authentication (`client_id` + `client_secret`) and returns `{"active": true/false}` with token metadata. Uses `verify_oauth_token()` so revoked tokens report as inactive.

### Expired Revocation Cleanup

`oauth2_service.cleanup_expired_revocations(db_session)` deletes revocation records for tokens that have already expired, preventing unbounded table growth.

### OIDC Discovery

`GET /.well-known/openid-configuration` returns a standard OpenID Connect discovery document when `oauth2_enabled` is `true` in `app.yaml`. Returns 404 otherwise.

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
    "administrator", "manage-users", "manage-pages", "modify-site", "manage-oauth-clients",
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
