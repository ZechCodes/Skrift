# OAuth2 Authorization Server

Skrift can act as an OAuth2 Authorization Server, allowing other applications (including other Skrift instances) to authenticate users via the Authorization Code grant with PKCE.

## Enabling the Server

Add to your `app.yaml`:

```yaml
oauth2_enabled: true
```

This registers the OAuth2 endpoints and enables the admin UI for client management.

## Managing Clients

OAuth2 clients are managed through the admin interface at `/admin/oauth-clients`. You need the `manage-oauth-clients` permission (granted to the `admin` role by default).

### Creating a Client

1. Navigate to `/admin/oauth-clients/new`
2. Enter a **Display Name** (shown on the consent screen)
3. Add **Redirect URIs** (one per line) — these must match exactly during authorization
4. Select **Allowed Scopes** — leave all unchecked to allow any registered scope
5. Click **Create Client**

The generated `client_id` and `client_secret` are shown in a flash message. **Save the secret immediately** — it won't be shown in full again.

### Editing a Client

From the client list, click **Edit** to change the display name, redirect URIs, allowed scopes, or active status. You can also regenerate the client secret (the old one stops working immediately).

### Deactivating a Client

Toggle the **Active** checkbox off. Inactive clients are rejected at all OAuth2 endpoints.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/oauth/authorize` | Authorization — shows consent screen |
| `POST` | `/oauth/authorize` | Processes consent — redirects with auth code |
| `POST` | `/oauth/token` | Token exchange — auth code or refresh token |
| `GET` | `/oauth/userinfo` | User claims — requires Bearer access token |
| `POST` | `/oauth/revoke` | Token revocation (RFC 7009) |
| `POST` | `/oauth/introspect` | Token introspection (RFC 7662) |
| `GET` | `/.well-known/openid-configuration` | OIDC Discovery document |

### Authorization (`GET /oauth/authorize`)

Query parameters:

| Parameter | Required | Description |
|-----------|----------|-------------|
| `response_type` | Yes | Must be `code` |
| `client_id` | Yes | Registered client ID |
| `redirect_uri` | Yes | Must match a registered URI |
| `scope` | No | Space-separated scope list |
| `state` | No | Opaque state for CSRF protection |
| `code_challenge` | Conditional | Required for public clients |
| `code_challenge_method` | Conditional | Must be `S256` |

If the user is not logged in, they are redirected to `/auth/login` with the full authorize URL preserved. After login, they return to the consent screen.

### Token Exchange (`POST /oauth/token`)

**Authorization Code grant** (`grant_type=authorization_code`):

| Parameter | Required | Description |
|-----------|----------|-------------|
| `grant_type` | Yes | `authorization_code` |
| `code` | Yes | Authorization code from redirect |
| `redirect_uri` | Yes | Must match the authorize request |
| `client_id` | Yes | Client identifier |
| `client_secret` | Conditional | Required for confidential clients |
| `code_verifier` | Conditional | Required when PKCE was used |

**Refresh Token grant** (`grant_type=refresh_token`):

| Parameter | Required | Description |
|-----------|----------|-------------|
| `grant_type` | Yes | `refresh_token` |
| `refresh_token` | Yes | Current refresh token |
| `client_id` | Yes | Client identifier |
| `client_secret` | Conditional | Required for confidential clients |

Refresh uses **token rotation** — the old refresh token is revoked and a new pair is issued.

### UserInfo (`GET /oauth/userinfo`)

Requires `Authorization: Bearer <access_token>` header. Returns claims filtered by the granted scopes:

| Scope | Claims Returned |
|-------|----------------|
| `openid` | `sub` |
| `profile` | `name`, `picture` |
| `email` | `email` |

If no scopes were specified, all claims are returned for backwards compatibility.

### Revocation (`POST /oauth/revoke`)

| Parameter | Required | Description |
|-----------|----------|-------------|
| `token` | Yes | The token to revoke |

Always returns HTTP 200, even for invalid tokens (per RFC 7009).

### Introspection (`POST /oauth/introspect`)

| Parameter | Required | Description |
|-----------|----------|-------------|
| `token` | Yes | The token to introspect |
| `client_id` | Yes | Authenticating client ID |
| `client_secret` | Conditional | Required for confidential clients |

Returns `{"active": true, "token_type": "...", "sub": "...", "scope": "...", "exp": ...}` for valid tokens, or `{"active": false}` for invalid/revoked tokens.

## Scopes

Built-in scopes:

| Scope | Description | Claims |
|-------|-------------|--------|
| `openid` | Verify your identity | `sub` |
| `profile` | Access your name and picture | `name`, `picture` |
| `email` | Access your email address | `email` |

### Custom Scopes

Register additional scopes in your application startup:

```python
from skrift.auth.scopes import register_scope

register_scope("custom", "Access custom data", claims=["custom_field"])
```

Custom scopes appear in the admin UI's scope checkboxes and are validated during authorization.

## Token Architecture

Tokens are HMAC-SHA256 signed JSON payloads (not JWT). Each token contains:

- `type` — prevents cross-use (`code`, `access`, `refresh`)
- `jti` — unique ID for revocation tracking
- `exp` — expiration timestamp

| Token | Lifetime |
|-------|----------|
| Authorization code | 10 minutes |
| Access token | 15 minutes |
| Refresh token | 30 days |

## PKCE

Only S256 is supported. Public clients (no `client_secret`) **must** use PKCE. Confidential clients may optionally use it.

## Connecting a Spoke Site

On the spoke Skrift instance, configure the `skrift` auth provider:

```yaml
auth:
  redirect_base_url: "https://spoke.example.com"
  providers:
    skrift:
      server_url: "https://hub.example.com"
      client_id: "<client_id from hub>"
      client_secret: "<client_secret from hub>"
      scopes: ["openid", "profile", "email"]
```

The spoke will redirect users to the hub for authentication and receive user data back via the standard OAuth2 flow.

## Security Considerations

- Tokens are **signed, not encrypted** — do not store secrets in token payloads
- Redirect URIs are strictly matched (no wildcards)
- Refresh token rotation limits the window for token reuse
- The consent form uses CSRF protection
- Inactive clients are rejected at all endpoints
- OIDC discovery is only served when `oauth2_enabled` is `true`

## See Also

- [Auth Providers](auth-providers.md) — OAuth provider configuration (client side)
- [Security Features](security-features.md) — CSRF, rate limiting, security headers
