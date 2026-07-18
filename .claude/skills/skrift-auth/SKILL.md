---
name: skrift-auth
description: "Skrift authentication and identity — OAuth login, sessions, guards/roles/permissions, and OAuth2 Authorization Server (hub/spoke federation)."
---

# Skrift Auth & Identity

OAuth login, session management, role-based authorization, and an optional OAuth2 Authorization Server for hub/spoke identity federation.

## OAuth Login Flow

```
/auth/{provider}/login → Provider → /auth/{provider}/callback → Session created
```

### Provider Configuration

```yaml
auth:
  redirect_base_url: "https://example.com"
  allowed_redirect_domains: []
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
      scopes: ["openid", "email", "profile"]
```

Available provider types: `google`, `github`, `microsoft`, `discord`, `facebook`, `twitter`, `skrift`, `dummy`.

### Provider Key vs Provider Type

By default, the config key **is** the provider type. An optional `provider` field decouples them, allowing custom keys and multiple instances:

```yaml
auth:
  providers:
    google: { client_id: ... }                              # key IS the type
    hub1: { provider: skrift, server_url: https://h1.com, client_id: ... }
    hub2: { provider: skrift, server_url: https://h2.com, client_id: ... }
    sso:  { provider: myapp.auth.SSOProvider, client_id: ... }  # dotted import
```

The `provider` field is consumed during config parsing. `AuthConfig.get_provider_type(key)` resolves key → type. `OAuthAccount.provider` stores the config **key** (not type) for multi-instance disambiguation.

Custom provider classes must subclass `OAuthProvider` and define a `provider_info` class attribute of type `OAuthProviderInfo`.

## Session Management

Client-side encrypted cookies (Litestar's CookieBackendConfig):
- 7-day expiry, HttpOnly, Secure (production), SameSite=Lax
- Configure `session.cookie_domain` for cross-subdomain sharing (see `/skrift-multisite`)

## Guard System

Protect routes with `auth_guard`, `Permission`, and `Role`:

```python
from skrift.auth import auth_guard, Permission, Role

class AdminController(Controller):
    path = "/admin"
    guards = [auth_guard, Permission("manage-pages")]
```

### Combining Guards

```python
# AND: Both required
guards = [auth_guard, Permission("edit") & Permission("publish")]

# OR: Either sufficient
guards = [auth_guard, Role("admin") | Role("editor")]
```

### Per-Route Guards

```python
class ArticleController(Controller):
    path = "/articles"
    guards = [auth_guard]

    @get("/")
    async def list_articles(self, db_session: AsyncSession): ...

    @post("/", guards=[Permission("create-articles")])
    async def create_article(self, db_session: AsyncSession, data: ArticleCreate): ...

    @delete("/{id:uuid}", guards=[Permission("delete-articles")])
    async def delete_article(self, db_session: AsyncSession, id: UUID): ...
```

## Built-in Roles

| Role | Permissions | Notes |
|------|-------------|-------|
| `admin` | `administrator`, `manage-users`, `manage-pages`, `modify-site`, `manage-oauth-clients`, `manage-api-keys` | Bypasses all checks |
| `editor` | `view-drafts`, `manage-pages`, `create-pages`, `manage-media` | All pages |
| `author` | `view-drafts`, `edit-own-pages`, `delete-own-pages`, `create-pages`, `upload-media` | Own pages |
| `moderator` | `view-drafts`, `manage-pages`, `create-pages`, `manage-media` | Moderate content |

### Custom Role Registration

```python
from skrift.auth import register_role

register_role(
    "contributor",
    "create-articles", "edit-own-articles",
    display_name="Contributor",
    description="Can create and edit their own articles",
)
```

## Controller with Session Access

```python
class AuthController(Controller):
    path = "/auth"

    @get("/profile")
    async def profile(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        user_id = request.session.get("user_id")
        if not user_id:
            raise NotAuthorizedException()
        user = await user_service.get_by_id(db_session, UUID(user_id))
        return TemplateResponse("auth/profile.html", context={"user": user})

    @post("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.session.clear()
        return Redirect("/")
```

## OAuth Token Persistence

On every login, `access_token` and `refresh_token` from the provider are saved on the `OAuthAccount` record. Tokens are refreshed (overwritten) on every login. Skrift does not auto-refresh expired tokens.

---

## OAuth2 Authorization Server

Skrift can act as an OAuth2 Authorization Server (hub) so other Skrift instances (spokes) authenticate users against it. Authorization Code grant with PKCE (S256 only).

### Hub/Spoke Flow

```
Spoke Site                          Hub Site (OAuth2 Server)
──────────                          ────────────────────────
User clicks "Login with Skrift"
    │
    ├──→ GET /oauth/authorize ──────→ Show consent screen
    │                                  (or redirect to login first)
    │
    │    ◄── Redirect with ?code= ◄── User clicks "Allow"
    │
    ├──→ POST /oauth/token ─────────→ Validate code + PKCE
    │    ◄── { access_token, ... } ◄── Return token pair
    │
    ├──→ GET /oauth/userinfo ───────→ Validate access token
    │    ◄── { sub, email, name } ◄── Return user claims
    │
    └──→ Session created on spoke
```

### Hub Configuration

```yaml
oauth2_enabled: true
```

When enabled: `OAuth2Controller` is auto-registered, OAuth token endpoints are CSRF-exempt, `/.well-known/openid-configuration` + `/.well-known/oauth-authorization-server` are active, and admin shows "OAuth Clients" (requires `manage-oauth-clients` permission). Add `oauth2_dynamic_registration_enabled: true` to open `POST /oauth/register` for machine clients (e.g. Claude custom connectors); it 404s otherwise.

Clients are managed via admin UI at `/admin/oauth-clients`:

| Action | Path | Method |
|--------|------|--------|
| List | `/admin/oauth-clients` | GET |
| Create | `/admin/oauth-clients/new` | GET/POST |
| Edit | `/admin/oauth-clients/{id}/edit` | GET/POST |
| Delete | `/admin/oauth-clients/{id}/delete` | POST |
| Regenerate secret | `/admin/oauth-clients/{id}/regenerate-secret` | POST |

### Spoke Configuration

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

Multiple hubs: use the `provider` field to decouple key from type (see Provider Key vs Provider Type above).

### Hub Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/oauth/authorize` | Consent screen (redirects to login if unauthenticated) |
| `POST` | `/oauth/authorize` | Issue auth code via redirect |
| `POST` | `/oauth/token` | Exchange code/refresh token for access/refresh tokens |
| `GET` | `/oauth/userinfo` | Scope-filtered user claims (Bearer token) |
| `POST` | `/oauth/revoke` | Revoke a token (RFC 7009) |
| `POST` | `/oauth/introspect` | Token introspection (RFC 7662, requires client auth) |
| `POST` | `/oauth/register` | Dynamic Client Registration (RFC 7591) — gated by `oauth2_dynamic_registration_enabled` |
| `GET` | `/oauth/jwks` | Public JWK Set for ES256 access-token verification |
| `GET` | `/.well-known/openid-configuration` | OIDC Discovery |
| `GET` | `/.well-known/oauth-authorization-server` | Authorization Server Metadata (RFC 8414) |

### Scope Registry

```python
from skrift.auth.scopes import register_scope

# Built-in: openid (sub), profile (name, picture), email (email)
register_scope("custom", "Access custom data", claims=["custom_field"])
```

Scopes control authorization (validated against client's `allowed_scopes`) and claims filtering (`/oauth/userinfo` returns only granted scope claims).

### Token Architecture

| Token | TTL | Signing | Payload / claims |
|-------|-----|---------|------------------|
| Auth code | 10 min | HMAC-SHA256 (`secret_key`) | user_id, email, name, client_id, redirect_uri, scope, code_challenge, resource |
| Access token | 15 min (`oauth2_access_token_ttl`) | ES256 JWT (rotating EC P-256 key) | iss, sub, client_id, scope, aud (when `resource` requested), iat, exp, jti |
| Refresh token | 30 days | HMAC-SHA256 (`secret_key`) | user_id, client_id, scope, family_id, resource |

Access tokens are **ES256 JWTs** (RFC 9068) verifiable offline against `GET /oauth/jwks`; codes and refresh tokens stay HMAC and never leave the client↔server exchange. HMAC tokens include a `jti` for revocation. Refresh exchange rotates (old token revoked; reuse of a rotated token mass-revokes its `family_id`). Signing keys rotate via `oauth2_signing_key_service.rotate()` / `prune_expired()`.

### Remembered Consent

`ConsentGrant` (unique per user + client) records approved scopes. `/oauth/authorize` skips the consent screen and issues a code directly when a stored grant covers all requested scopes; broader scopes re-prompt and widen the grant on approval. Grants cascade-delete when the client is deleted or pruned. Service: `skrift/db/services/oauth2_consent_service.py` (`find_grant`, `upsert_grant`, `revoke_grant`, `delete_grants_for_clients`).

### PKCE

S256 only. **Required** for all clients (OAuth 2.1); `plain` is rejected.

### MCP / Bearer JWT guard

Downstream MCP resource servers protect routes with `BearerJWTAuth(audience=..., scopes=[...])` from `skrift/auth/bearer.py`, listed in a route's `guards` alongside `auth_guard`. It verifies the JWT against the JWKS, checks `iss`/`exp`/`aud`, enforces scopes, and resolves `sub` to the same `UserPermissions` identity as the session/API-key paths (so `Permission()`/`Role()` compose). Fail-closed 401 with `WWW-Authenticate: Bearer error="invalid_token"` (or `insufficient_scope`). See `docs/reference/mcp-identity-provider.md`.

## Security Notes

- Tokens are signed, not encrypted — do not store secrets in payloads
- `redirect_uri` validated against client's registered list (strict match)
- Consent form uses CSRF protection
- `hmac.compare_digest` for constant-time signature comparison
- Inactive clients (`is_active=False`) rejected at all endpoints

## Key Files

| File | Purpose |
|------|---------|
| `skrift/auth/` | Guards, roles, permissions |
| `skrift/auth/providers.py` | OAuth provider classes, `get_oauth_provider()`, dynamic import |
| `skrift/auth/tokens.py` | `create_signed_token()` / `verify_signed_token()` (HMAC codes/refresh) |
| `skrift/auth/jwt_tokens.py` | `create_access_token_jwt()` / `verify_access_token_jwt()` (ES256 access tokens) |
| `skrift/auth/bearer.py` | `BearerJWTAuth` guard marker for MCP resource servers |
| `skrift/auth/scopes.py` | Scope registry |
| `skrift/auth/oauth_account_service.py` | `find_or_create_oauth_user()` with token persistence |
| `skrift/controllers/auth.py` | OAuth login/callback controller |
| `skrift/controllers/oauth2.py` | OAuth2Controller — authorize, token, userinfo, revoke, introspect, register, jwks |
| `skrift/config.py` | `AuthConfig`, `SkriftProviderConfig`, `oauth2_enabled`, `oauth2_dynamic_registration_enabled`, `oauth2_issuer`, `oauth2_access_token_ttl`, `oauth2_allowed_resources` |
| `skrift/db/models/user.py` | `User` model |
| `skrift/db/models/oauth_account.py` | `OAuthAccount` model |
| `skrift/db/models/oauth2_client.py` | `OAuth2Client` model |
| `skrift/db/models/oauth2_consent_grant.py` | `ConsentGrant` model (remembered consent) |
| `skrift/db/models/oauth2_signing_key.py` | `OAuth2SigningKey` model (rotating ES256 keys) |
| `skrift/db/models/revoked_token.py` | `RevokedToken` model |
| `skrift/db/services/oauth2_service.py` | Client CRUD, revocation |
| `skrift/db/services/oauth2_consent_service.py` | Remembered-consent CRUD + client-delete cascade |
| `skrift/db/services/oauth2_signing_key_service.py` | Signing-key lifecycle (`rotate`, `prune_expired`) |
| `skrift/admin/oauth2_clients.py` | Admin UI for OAuth2 clients |
| `skrift/setup/providers.py` | `OAuthProviderInfo` definitions |
