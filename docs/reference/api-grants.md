# API Permission Grants

Skrift can issue user-bound API keys to third-party services through an OAuth-style permission grant flow. The flow lets a service request a scoped `sk_...` API key for a specific user, while Skrift shows the user readable permission names and descriptions before issuing the key.

Grant-issued keys use the existing API key authenticator and route guards. A route protected with `APIKeyAuth()` or `APIKeyOnly()` sees the granted key like any other API key, with effective permissions limited to the key scope and the approving user's permissions.

## Configuration

API grants depend on API keys:

```yaml
api_keys:
  enabled: true

api_grants:
  enabled: true              # default: true when API keys are enabled
  discovery_enabled: false   # opt in to /.well-known/skrift
```

When `api_grants.discovery_enabled` is true, Skrift serves `GET /.well-known/skrift` with public grant metadata and the permissions that are explicitly safe for anonymous service requests.

## Permission Metadata

Declare permission metadata in application startup code:

```python
from skrift.auth.permissions import (
    ALLOW_ANONYMOUS_SERVICE,
    REQUIRE_KNOWN_SERVICE,
    REQUIRE_ELEVATED_SECURITY,
    register_permission,
)

register_permission(
    "read-profile",
    display_name="Read Profile",
    description="Read your public profile data.",
    service_clearance=ALLOW_ANONYMOUS_SERVICE,
)

register_permission(
    "sync-records",
    display_name="Sync Records",
    description="Read and sync records this service already has access to.",
    service_clearance=REQUIRE_KNOWN_SERVICE,
)

register_permission(
    "export-private-data",
    display_name="Export Private Data",
    description="Export private account data after confirming your identity.",
    service_clearance=REQUIRE_ELEVATED_SECURITY,
)
```

The default clearance is `disallow-api-grants`. This is intentional: adding a new route permission never makes it externally requestable until the application explicitly opts it in.

## Service Clearance

| Clearance | Meaning |
|-----------|---------|
| `disallow-api-grants` | Default. The permission cannot be requested through the third-party grant flow. |
| `allow-anonymous-service` | Any external service may request this permission without already having a service API key. The request must include a `service_name` for the user-facing grant screen. |
| `require-known-service` | The requester must authenticate with an existing service API key, and the requested permissions must be a subset of that key's effective permissions. |
| `require-elevated-security` | Same as known-service, plus the user must complete the browser authorization and consent flow before Skrift issues the key. |

For mixed permission requests, Skrift enforces the strictest requested clearance. If any requested permission is `disallow-api-grants`, Skrift rejects the request and does not render a consent screen.

## Protocol

The grant protocol uses authorization-code semantics with PKCE. The raw API key is never returned through a browser redirect.

### Anonymous-Service Request

For permissions marked `allow-anonymous-service`, a service may send the user directly to the authorization endpoint:

```text
GET /api/grants/authorize?
  response_type=code&
  redirect_uri=https%3A%2F%2Fservice.example%2Fcallback&
  permissions=read-profile&
  service_name=Example%20Service&
  state=opaque-state&
  code_challenge=...&
  code_challenge_method=S256
```

Skrift redirects unauthenticated users through `/auth/login`, then shows a consent screen with the registered permission names and descriptions.

### Known-Service Request

Known-service and elevated-security requests must start with a server-to-server request. This keeps the service API key in the `Authorization` header instead of placing it in a browser URL.

```bash
curl -X POST https://site.example/api/grants/request \
  -H "Authorization: Bearer sk_existing_service_key" \
  -H "Content-Type: application/json" \
  -d '{
    "redirect_uri": "https://service.example/callback",
    "permissions": ["sync-records"],
    "service_name": "Example Service",
    "code_challenge": "...",
    "code_challenge_method": "S256",
    "state": "opaque-state"
  }'
```

Skrift verifies that:

- the bearer key is active;
- the bearer key is a service principal;
- the requested permissions are a subset of the bearer key's effective permissions;
- none of the requested permissions are disallowed.

If validation passes, Skrift returns:

```json
{
  "request_token": "...",
  "authorize_url": "/api/grants/authorize?request_token=...",
  "expires_in": 600
}
```

Send the user's browser to `authorize_url`.

### Token Exchange

After consent, Skrift redirects to the service's `redirect_uri` with a one-time code:

```text
https://service.example/callback?code=...&state=opaque-state
```

The service exchanges the code for an API key:

```bash
curl -X POST https://site.example/api/grants/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "code=..." \
  -d "redirect_uri=https://service.example/callback" \
  -d "code_verifier=..."
```

For known-service or elevated-security grants, include the same service API key used to create the request:

```bash
-H "Authorization: Bearer sk_existing_service_key"
```

Response:

```json
{
  "key": "sk_...",
  "refresh_token": "skr_...",
  "key_prefix": "sk_...",
  "token_type": "bearer",
  "principal_type": "service",
  "service_name": "Example Service",
  "permissions": ["sync-records"],
  "expires_at": "2027-05-20T00:00:00+00:00",
  "refresh_token_expires_at": "2026-06-19T00:00:00+00:00"
}
```

The issued key is always bound to the approving user and has `principal_type="service"`. Permission checks still intersect the key's scope with the user's current permissions.

Grant extensions may add consent-screen fragments and API-key constraints through hooks. Constraints are signed into the authorization code and stored on the issued key, which lets optional components bind a grant to choices such as source origin, resource type, or delivery behavior without hard-coding those concerns into the core grant protocol.

## Discovery

When enabled, `GET /.well-known/skrift` returns:

```json
{
  "skrift": true,
  "version": 1,
  "api_grants": {
    "authorization_endpoint": "https://site.example/api/grants/authorize",
    "request_endpoint": "https://site.example/api/grants/request",
    "token_endpoint": "https://site.example/api/grants/token",
    "code_challenge_methods_supported": ["S256"],
    "anonymous_permissions": [
      {
        "slug": "read-profile",
        "display_name": "Read Profile",
        "description": "Read your public profile data."
      }
    ]
  }
}
```

Only `allow-anonymous-service` permissions are advertised.

## Security Notes

- Use PKCE S256 for every grant request.
- Do not put service API keys in browser URLs; use `/api/grants/request`.
- Raw `sk_...` and `skr_...` secrets are returned only from `/api/grants/token`.
- Authorization codes are short-lived and one-time use.
- Grant-issued keys rotate through the normal `/api/auth/refresh` endpoint.
- Revoking the approving user or removing the user's permissions reduces or removes the key's effective access.

## See Also

- [API Keys](api-keys.md)
- [Protecting Routes](../guides/protecting-routes.md)
- [OAuth2 Server](oauth2-server.md)
