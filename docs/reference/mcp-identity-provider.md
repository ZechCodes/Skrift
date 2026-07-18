# MCP Identity Provider

Skrift's [OAuth2 Authorization Server](oauth2-server.md) doubles as an identity provider for [Model Context Protocol](https://modelcontextprotocol.io) (MCP) resource servers — including **Claude custom connectors**. A connector runs the standard Authorization Code + PKCE flow against Skrift, self-registers through Dynamic Client Registration, and receives an **ES256 JWT access token** that any downstream MCP resource server can verify offline against Skrift's published JWKS.

This page covers the pieces specific to the MCP use case: enabling the server for connectors, the audience naming convention, signing-key rotation, remembered consent, and a worked downstream resource-server example. For the endpoint reference and hub/spoke federation, see [OAuth2 Server](oauth2-server.md).

## Architecture

```
Claude custom connector          Skrift (Authorization Server)      MCP resource server
        │                                    │                              │
        │  POST /oauth/register  ───────────►│  (Dynamic Client Reg.)       │
        │◄───────────── client_id ───────────│                              │
        │                                    │                              │
        │  /oauth/authorize (PKCE) ─────────►│  login + consent             │
        │◄───────────── code ────────────────│                              │
        │  POST /oauth/token ───────────────►│  issue ES256 JWT access token│
        │◄───────────── access_token ────────│                              │
        │                                    │                              │
        │  MCP request + Bearer <JWT>  ──────┼─────────────────────────────►│
        │                                    │   verify via GET /oauth/jwks ◄┤
        │◄─────────────── MCP response ──────┼──────────────────────────────│
```

The connector and the resource server can be the same deployment or separate ones. The trust anchor between them is Skrift's JWKS endpoint plus the token's `iss` and `aud` claims.

## Enabling the Server for Connectors

Two flags in `app.yaml`:

```yaml
oauth2_enabled: true                     # registers the OAuth2 endpoints
oauth2_dynamic_registration_enabled: true  # opens POST /oauth/register
```

`oauth2_dynamic_registration_enabled` is gated on top of `oauth2_enabled`; the registration endpoint returns `404` while it is `false`. Enable it only when you want machine clients such as Claude connectors to self-register.

Recommended companion settings:

```yaml
oauth2_issuer: "https://id.example.com"   # stable issuer baked into every token's `iss`
oauth2_access_token_ttl: 900              # access-token lifetime in seconds (default 15 min)
oauth2_allowed_resources:                 # allowlist of canonical resource identifiers
  - "https://app.example.com/mcp"
```

| Setting | Default | Purpose |
|---------|---------|---------|
| `oauth2_enabled` | `false` | Registers the OAuth2 Authorization Server endpoints |
| `oauth2_dynamic_registration_enabled` | `false` | Opens `POST /oauth/register` (RFC 7591) |
| `oauth2_issuer` | `""` | Issuer (`iss`) claim; empty falls back to the request base URL |
| `oauth2_access_token_ttl` | `900` | Access-token lifetime in seconds |
| `oauth2_allowed_resources` | `[]` | RFC 8707 resource allowlist; empty allows any absolute URI |
| `oauth2_dynamic_registration_ip_limit` | `20` | Max dynamic registrations per IP per window |
| `oauth2_dynamic_registration_ip_window_seconds` | `3600` | Registration rate-limit window |
| `oauth2_dynamic_client_max_age_days` | `7` | Unused dynamic clients are pruned after this age |

!!! warning "Set a stable issuer in production"
    Downstream resource servers verify the `iss` claim against a configured value. If `oauth2_issuer` is left empty, `iss` is derived from the incoming request's base URL, which can vary behind proxies or across hostnames and will break verification. Always pin `oauth2_issuer` for any deployment that issues tokens to resource servers.

### Registering a Claude custom connector

When you point a Claude custom connector at `https://id.example.com`, it discovers the endpoints from `/.well-known/oauth-authorization-server` and self-registers:

```http
POST /oauth/register
Content-Type: application/json

{
  "client_name": "Claude",
  "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "scope": "openid profile email"
}
```

Dynamic clients are always **public** (`token_endpoint_auth_method` must be `none`) and authenticate with PKCE alone. Redirect URIs must be `https` (loopback `http://127.0.0.1` / `http://localhost` is the only exception). The connector then runs the ordinary authorize/token flow described in [OAuth2 Server](oauth2-server.md).

## Audience Naming Convention

An MCP access token should name the resource server it is meant for so that server can reject tokens minted for anyone else. Skrift implements this with the RFC 8707 `resource` indicator, which becomes the JWT `aud` claim.

**Use the resource server's canonical resource identifier as the audience** — the stable, absolute HTTPS URL of the MCP endpoint, with no fragment and no trailing slash:

```
https://app.example.com/mcp
```

The connector passes it as `resource` on both the authorize and token requests:

```
GET /oauth/authorize?...&resource=https://app.example.com/mcp
POST /oauth/token       ...&resource=https://app.example.com/mcp
```

Skrift validates the value against `oauth2_allowed_resources` (when configured) and stamps it into the token's `aud`. Rules:

- The audience is a **canonical resource identifier**, not a random string — the resource server compares the token's `aud` to its own identity.
- Keep it **absolute and fragment-free**; Skrift rejects relative or fragment-bearing resources with `invalid_target`.
- A token-endpoint `resource` may **narrow** but never **widen** the grant: it must equal the value authorized at `/oauth/authorize` (or restrict an otherwise unrestricted grant).
- Populate `oauth2_allowed_resources` with the exact identifiers you recognize so a client cannot request a token for an arbitrary audience.

A token minted for `https://app.example.com/mcp` carries `"aud": "https://app.example.com/mcp"`. The resource server MUST verify that its own identifier appears in `aud` (see the worked example below).

## Access-Token Format and JWKS

Access tokens are **ES256 JWTs** (RFC 9068 `at+jwt` profile) signed by a rotating EC P-256 key. Authorization codes and refresh tokens stay HMAC-signed and never leave the client↔server exchange. An access-token JWT carries:

| Claim | Meaning |
|-------|---------|
| `iss` | The issuer (`oauth2_issuer` or request base URL) |
| `sub` | The Skrift user id (UUID) |
| `aud` | The resource identifier, when a `resource` was requested |
| `client_id` | The client the token was issued to |
| `scope` | Space-separated granted scopes |
| `iat` / `exp` | Issued-at / expiry |
| `jti` | Unique id, used for revocation checks |

Public keys are published at `GET /oauth/jwks`. Resource servers fetch and cache this JWKS to verify token signatures offline — no network round-trip to Skrift per request. The active key signs new tokens; retired keys stay published until every token they signed has expired.

## Signing-Key Rotation

Rotation lives in `skrift.db.services.oauth2_signing_key_service`:

| Function | Effect |
|----------|--------|
| `get_or_create_active_key(db_session)` | Returns the active signing key, generating the first one on demand |
| `rotate(db_session)` | Generates a fresh key, marks it active, and retires the previous one (kept in the JWKS until it ages out) |
| `list_jwks_keys(db_session)` | Public JWKs for every still-published key — what `/oauth/jwks` serves |
| `prune_expired(db_session, now, access_token_ttl, clock_skew)` | Deletes retired keys older than `retired_at + access_token_ttl + clock_skew`, once no live token could still be signed by them |

A first key is created automatically the first time a token is minted or `/oauth/jwks` is requested, so no setup is required to start issuing tokens.

To rotate on a schedule, run `rotate` then `prune_expired` from a maintenance task, mirroring the dynamic-client pruning worker in `skrift/oauth2_maintenance.py`:

```python
from datetime import datetime, timezone

from skrift.db.services import oauth2_signing_key_service


async def rotate_signing_keys(session_maker, *, access_token_ttl: int) -> None:
    async with session_maker() as db_session:
        await oauth2_signing_key_service.rotate(db_session)
        await oauth2_signing_key_service.prune_expired(
            db_session,
            now=datetime.now(tz=timezone.utc),
            access_token_ttl=access_token_ttl,
            clock_skew=60,
        )
```

Rotation is safe while tokens are live: because retired keys remain in the JWKS until they age past the access-token lifetime, any token signed just before a rotation still verifies until it expires. Rotate on a cadence longer than `oauth2_access_token_ttl` (e.g. daily or weekly), and rotate immediately if a signing key is ever suspected compromised — followed by revoking outstanding tokens.

## Remembered Consent

Once a user approves a client's scopes, Skrift records the grant (`ConsentGrant`, keyed uniquely on user + client). On the next `/oauth/authorize` request from the same user for that client:

- If the stored grant **covers every requested scope**, the consent screen is skipped and the authorization code is issued directly — PKCE and the resource indicator are still bound to the code.
- If the request asks for **scopes beyond** the grant, the consent screen is shown again to approve the wider set. Approving it **widens** the stored grant (scopes are unioned, never dropped).

This makes reconnecting a Claude connector a one-click (or zero-click) experience after the first authorization, without weakening scope enforcement.

Grants are removed automatically when their client is deleted — both the admin delete action and the dynamic-client pruning sweep cascade to the client's consent grants. A grant can also be revoked programmatically:

```python
from skrift.db.services import oauth2_consent_service

await oauth2_consent_service.revoke_grant(db_session, user_id, client_id)
```

## Downstream Resource Server

A resource server (your MCP endpoint) does two things: advertise where its authorization server lives, and reject requests that don't carry a valid, correctly-audienced token.

### 1. Protected-resource metadata

Publish [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) metadata so clients can discover the authorization server from a `401`:

```http
GET /.well-known/oauth-protected-resource

200 OK
Content-Type: application/json

{
  "resource": "https://app.example.com/mcp",
  "authorization_servers": ["https://id.example.com"],
  "scopes_supported": ["openid", "profile", "email"],
  "bearer_methods_supported": ["header"]
}
```

`resource` is the canonical identifier used as the token `aud`; `authorization_servers` points at the Skrift issuer, whose `/.well-known/oauth-authorization-server` the client reads to find the authorize, token, register, and JWKS endpoints.

### 2. Challenge unauthenticated requests

An MCP request with no (or a bad) token gets a `401` whose `WWW-Authenticate` header points at the metadata, per RFC 9728:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://app.example.com/.well-known/oauth-protected-resource",
                  error="invalid_token"
```

### 3. Verify the token with the Bearer guard

When the resource server is itself a Skrift instance, protect routes with the reusable **Bearer JWT guard** (`skrift.auth.bearer`). List the marker in a route's `guards`; it verifies the JWT against the JWKS, checks `iss`, `exp`, and that the configured audience appears in `aud`, enforces the required scopes, resolves `sub` to a `User`, and populates the same identity the session and API-key paths use — so `Permission()` / `Role()` guards compose unchanged.

```python
from litestar import Controller, get

from skrift.auth.bearer import BearerJWTAuth
from skrift.auth.guards import auth_guard
from skrift.auth.permissions import Permission


class MCPResourceController(Controller):
    path = "/mcp"

    @get(
        "/context",
        guards=[
            auth_guard,
            BearerJWTAuth(audience="https://app.example.com/mcp", scopes=["profile"]),
            Permission("read-context"),
        ],
    )
    async def context(self) -> dict:
        return {"ok": True}
```

Failure modes are fail-closed and return `401`:

- Missing / expired / tampered token, or wrong `aud` / `iss` → `WWW-Authenticate: Bearer error="invalid_token"`.
- Valid token missing a required scope → `WWW-Authenticate: Bearer error="insufficient_scope"`.

The guard distinguishes a JWT (three dot-separated segments) from an API key (`sk_` prefix), so a JWT never satisfies an API-key-only route and vice-versa.

For a non-Skrift resource server, verify tokens with any JOSE library: fetch `https://id.example.com/oauth/jwks`, verify the ES256 signature by `kid`, and assert `iss == "https://id.example.com"`, `exp` is in the future, and your resource identifier is in `aud`.

## See Also

- [OAuth2 Server](oauth2-server.md) — full endpoint reference, scopes, hub/spoke federation
- [Auth Providers](auth-providers.md) — configuring Skrift as an OAuth client
- [API Keys](api-keys.md) — the other Bearer scheme (`sk_` keys) the guard understands
