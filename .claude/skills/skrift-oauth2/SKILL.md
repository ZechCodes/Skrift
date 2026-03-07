---
name: skrift-oauth2
description: "Skrift OAuth2 Authorization Server — hub/spoke identity federation, Authorization Code + PKCE flow, token architecture, and Skrift auth provider."
---

# Skrift OAuth2 Authorization Server

Skrift can act as an OAuth2 Authorization Server (hub) so other Skrift instances (spokes) can authenticate users against it. Supports Authorization Code grant with PKCE (S256 only). Tokens are stdlib HMAC-SHA256 signed — no JWT library required.

## Hub/Spoke Flow

```
Spoke Site                          Hub Site (OAuth2 Server)
──────────                          ────────────────────────
User clicks "Login with Skrift"
    │
    ├──→ GET /oauth/authorize ──────→ Show consent screen
    │                                  (or redirect to /auth/login first)
    │
    │    ◄── Redirect with ?code= ◄── User clicks "Allow"
    │
    ├──→ POST /oauth/token ─────────→ Validate code + PKCE
    │    ◄── { access_token, ... } ◄── Return token pair
    │
    ├──→ GET /oauth/userinfo ───────→ Validate access token
    │    ◄── { sub, email, name } ◄── Return user claims (scope-filtered)
    │
    └──→ Session created on spoke
```

## Hub Configuration (`app.yaml`)

Enable the OAuth2 server:

```yaml
oauth2_enabled: true
```

Clients are managed via the admin UI at `/admin/oauth-clients` (not in app.yaml). When `oauth2_enabled` is `true`:

- `OAuth2Controller` is auto-registered (`skrift/asgi.py`)
- `/oauth/token`, `/oauth/revoke`, `/oauth/introspect` are auto-excluded from CSRF
- `/.well-known/openid-configuration` returns OIDC discovery JSON
- The admin UI shows "OAuth Clients" in the sidebar (requires `manage-oauth-clients` permission)

### Client Management (Admin UI)

Clients are stored in the `oauth2_clients` database table, managed via `OAuth2ClientAdminController` at `/admin/oauth-clients`.

| Action | Path | Method |
|--------|------|--------|
| List clients | `/admin/oauth-clients` | GET |
| Create form | `/admin/oauth-clients/new` | GET |
| Create client | `/admin/oauth-clients/new` | POST |
| Edit form | `/admin/oauth-clients/{id}/edit` | GET |
| Update client | `/admin/oauth-clients/{id}/edit` | POST |
| Delete client | `/admin/oauth-clients/{id}/delete` | POST |
| Regenerate secret | `/admin/oauth-clients/{id}/regenerate-secret` | POST |

On creation, `client_id` and `client_secret` are auto-generated via `secrets.token_urlsafe()`. The secret is shown once in a flash message.

### OAuth2Client Model

`skrift/db/models/oauth2_client.py` — extends `Base` (UUIDAuditBase):

| Field | Type | Notes |
|-------|------|-------|
| `client_id` | `String(255)` | Unique, indexed, auto-generated |
| `client_secret` | `String(255)` | Auto-generated, empty = public client |
| `display_name` | `String(255)` | Shown on consent screen |
| `redirect_uris` | `Text` | Newline-delimited |
| `allowed_scopes` | `Text` | Newline-delimited; empty = all scopes allowed |
| `is_active` | `Boolean` | Default `True`; inactive clients are rejected |

Properties: `redirect_uri_list` and `allowed_scope_list` parse the text fields into `list[str]`.

## Spoke Configuration (`app.yaml`)

The spoke uses the `skrift` auth provider to point at the hub:

```yaml
auth:
  redirect_base_url: "https://spoke.example.com"
  providers:
    skrift:
      server_url: "https://hub.example.com"
      client_id: "spoke-site-1"
      client_secret: ""  # empty for public clients
      scopes: ["openid", "profile", "email"]
```

Config model: `SkriftProviderConfig`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `server_url` | `str` | required | Base URL of the hub Skrift instance |
| `client_id` | `str` | required | Must match a hub client's `client_id` |
| `client_secret` | `str` | `""` | Empty = public client |
| `scopes` | `list[str]` | `["openid", "profile", "email"]` | Requested scopes |

## Scope Registry

`skrift/auth/scopes.py` — dataclass + dict registry pattern (like `roles.py`):

```python
from skrift.auth.scopes import register_scope, SCOPE_DEFINITIONS, get_scope_definition

# Built-in scopes (registered at import time):
# openid  → claims: [sub]
# profile → claims: [name, picture]
# email   → claims: [email]

# Register a custom scope:
register_scope("custom", "Access custom data", claims=["custom_field"])
```

Scopes control two things:
1. **Authorization**: requested scopes are validated against the client's `allowed_scopes` during `/oauth/authorize`
2. **Claims filtering**: `/oauth/userinfo` only returns claims for the granted scopes

## Endpoints (Hub)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/oauth/authorize` | Show consent screen (or redirect to login if unauthenticated) |
| `POST` | `/oauth/authorize` | Process consent form — issue auth code via redirect |
| `POST` | `/oauth/token` | Exchange auth code or refresh token for access/refresh tokens |
| `GET` | `/oauth/userinfo` | Return scope-filtered user claims from a valid Bearer access token |
| `POST` | `/oauth/revoke` | Revoke a token (RFC 7009 — always returns 200) |
| `POST` | `/oauth/introspect` | Introspect a token (RFC 7662 — requires client auth) |
| `GET` | `/.well-known/openid-configuration` | OIDC Discovery document (on `SitemapController`) |

### Token Revocation (`POST /oauth/revoke`)

RFC 7009 compliant. Accepts `token` in form body. Always returns 200 (even for invalid tokens). Records the token's `jti` in `revoked_tokens` table. Revoked tokens are rejected by `verify_oauth_token()`.

### Token Introspection (`POST /oauth/introspect`)

RFC 7662 compliant. Requires `client_id` + `client_secret` for authentication. Returns `{"active": true/false, ...}` with token metadata when active.

### OIDC Discovery (`GET /.well-known/openid-configuration`)

Returns 404 when `oauth2_enabled` is `false`. Otherwise returns standard discovery JSON: issuer, all endpoint URLs, supported response types/grants/scopes/claims/code challenge methods.

## Token Architecture

All tokens use `base64(json_payload).base64(hmac_sha256_signature)` format. Signed with `settings.secret_key`. Every token includes a unique `jti` (UUID hex) for revocation tracking.

| Token | TTL | `type` field | Payload |
|-------|-----|-------------|---------|
| Auth code | 10 min | `"code"` | `user_id`, `email`, `name`, `picture_url`, `client_id`, `redirect_uri`, `scope`, `code_challenge`, `jti` |
| Access token | 15 min | `"access"` | `user_id`, `email`, `name`, `picture_url`, `client_id`, `scope`, `jti` |
| Refresh token | 30 days | `"refresh"` | `user_id`, `client_id`, `scope`, `jti` |

The `type` field prevents cross-use — a refresh token cannot be used as an access token.

Token exchange with `grant_type=refresh_token` performs **token rotation**: old refresh token is revoked, both access and refresh tokens are reissued.

### Token Verification

`verify_oauth_token(token, secret, db_session)` in `skrift/controllers/oauth2.py`:
1. Verifies HMAC signature and expiration (via `verify_signed_token`)
2. Checks `jti` against `revoked_tokens` table (via `oauth2_service.is_token_revoked`)
3. Returns payload dict or `None`

Used by: `userinfo`, `_handle_refresh_token`, `introspect`.

Auth codes use plain `verify_signed_token` (no revocation check — they're single-use by expiry).

## PKCE

- Only `S256` is supported (`code_challenge_method=S256`)
- **Required** for public clients (no `client_secret`)
- Optional but supported for confidential clients
- Verification: `base64url(sha256(code_verifier)) == code_challenge`
- The `code_challenge` is embedded in the auth code token and verified at `/oauth/token`

## SkriftProvider (Spoke Side)

`SkriftProvider` (`skrift/auth/providers.py:245`) handles the spoke's OAuth flow:

- `requires_pkce = True` — always sends PKCE parameters
- `resolve_url()` replaces `{server_url}` in provider URLs with the configured `server_url`
- `build_token_data()` omits `client_secret` from POST body when empty (public client)
- `extract_user_data()` maps hub's `sub` -> `oauth_id`, plus `email`, `name`, `picture`

Provider URLs are defined in `skrift/setup/providers.py` with `{server_url}` placeholders:
- `auth_url`: `{server_url}/oauth/authorize`
- `token_url`: `{server_url}/oauth/token`
- `userinfo_url`: `{server_url}/oauth/userinfo`

## Consent Template

`skrift/templates/oauth/authorize.html` — extends `auth/base.html`, shows client `display_name` (falls back to `client_id`) and scope descriptions from the scope registry. CSRF-protected form with Allow/Deny buttons. Customizable by overriding in project `templates/oauth/authorize.html`.

## Security Notes

- Tokens are **signed, not encrypted** — payload is base64-encoded and readable. Do not store secrets in token payloads.
- `redirect_uri` is validated against the client's registered list (strict match)
- Consent form uses CSRF protection (`csrf_field`)
- The `type` field in each token prevents cross-use (code vs access vs refresh)
- `hmac.compare_digest` used for constant-time signature comparison
- Token revocation via `jti` tracking in database
- Refresh token rotation: old refresh token is revoked on each use
- Inactive clients (`is_active=False`) are rejected at all endpoints

## Key Files

| File | Purpose |
|------|---------|
| `skrift/controllers/oauth2.py` | OAuth2Controller — authorize, token, userinfo, revoke, introspect |
| `skrift/auth/tokens.py` | `create_signed_token()` / `verify_signed_token()` — HMAC-SHA256 with `jti` |
| `skrift/auth/scopes.py` | Scope registry — `ScopeDefinition`, `register_scope()`, built-in scopes |
| `skrift/auth/providers.py` | `SkriftProvider` class (spoke-side OAuth flow) |
| `skrift/config.py` | `SkriftProviderConfig`, `oauth2_enabled` setting |
| `skrift/db/models/oauth2_client.py` | `OAuth2Client` model |
| `skrift/db/models/revoked_token.py` | `RevokedToken` model |
| `skrift/db/services/oauth2_service.py` | Client CRUD, revocation, token checks |
| `skrift/admin/oauth2_clients.py` | `OAuth2ClientAdminController` — admin UI |
| `skrift/controllers/sitemap.py` | `/.well-known/openid-configuration` discovery endpoint |
| `skrift/setup/providers.py` | `OAuthProviderInfo` for `skrift` (URL templates, fields) |
| `skrift/asgi.py` | Auto-registers `OAuth2Controller` when `oauth2_enabled` |
| `skrift/templates/oauth/authorize.html` | Consent screen template |
| `skrift/templates/admin/oauth2/list.html` | Admin client list |
| `skrift/templates/admin/oauth2/edit.html` | Admin client create/edit form |
| `tests/test_oauth2_server.py` | OAuth2 server endpoint tests (42 tests) |
