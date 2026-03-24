# API Keys

Skrift supports API key authentication for programmatic access. Keys are bearer tokens that can be scoped to specific permissions or roles, with built-in support for key rotation via refresh tokens.

## Configuration

```yaml
api_keys:
  enabled: true                      # default: true
  default_expiration_days: 365       # default key lifetime
  max_keys_per_user: 10              # per-user key limit
  refresh_token_expiration_days: 30  # refresh token lifetime
```

## Creating Keys

Admins manage API keys at **Admin > API Keys** (`/admin/api-keys`). Requires the `manage-api-keys` permission (included in the `admin` role).

When creating a key:

1. Select the **user** the key acts as
2. Set a **display name** and optional description
3. Optionally scope to specific **permissions** and/or **roles**
4. Optionally set an **expiration date**

The full API key (`sk_...`) and refresh token (`skr_...`) are shown **once** at creation. Store them securely.

## Authentication

Include the API key as a Bearer token:

```bash
curl -H "Authorization: Bearer sk_abc123..." https://example.com/api/pages
```

The key resolves to the associated user. If the key has scoped permissions, the effective permissions are the **intersection** of the user's permissions and the key's scoped permissions. If no scoping is set, the key inherits all of the user's permissions.

## Route Declaration

Routes must explicitly opt in to API key authentication using guard markers:

```python
from skrift.auth.guards import auth_guard, APIKeyAuth, APIKeyOnly, Permission

# Session only (default, unchanged)
@get("/admin/users", guards=[auth_guard, Permission("manage-users")])

# API-compatible: accepts both session cookies and API keys
@get("/api/pages", guards=[auth_guard, APIKeyAuth(), Permission("manage-pages")])

# API-only: rejects session auth, requires API key
@post("/api/v1/sync", guards=[auth_guard, APIKeyOnly(), Permission("manage-pages")])
```

Routes without `APIKeyAuth()` or `APIKeyOnly()` markers continue to require session authentication only.

## Key Rotation

API keys support zero-downtime rotation via refresh tokens. Send a POST request to exchange a refresh token for a new key:

```bash
curl -X POST https://example.com/api/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "skr_abc123..."}'
```

Response:

```json
{
  "key": "sk_newkey...",
  "refresh_token": "skr_newrefresh...",
  "key_prefix": "sk_newkey1234",
  "expires_at": "2027-03-24T00:00:00+00:00",
  "refresh_token_expires_at": "2026-04-23T00:00:00+00:00"
}
```

The old key and refresh token stop working immediately. The new key inherits all settings (scoping, expiration, user) from the original.

Admins can also manually rotate keys from the admin UI via the **Rotate Key** button.

## Key Lifecycle

| State | Description |
|-------|-------------|
| **Active** | Key is valid and can authenticate requests |
| **Expired** | Key's `expires_at` has passed; requests are rejected |
| **Revoked** | Admin set `is_active=False`; requests are rejected |
| **Deleted** | Key permanently removed from the database |

## Security

- Keys are stored as **SHA-256 hashes** — the raw key is never persisted
- The `sk_` prefix makes keys identifiable in logs and secret scanners
- Keys respect the user's `is_active` flag — deactivating a user invalidates all their keys
- Permission scoping prevents privilege escalation if a user's permissions change after key creation
- The `/api/auth/refresh` endpoint is CSRF-exempt and rate-limited

## Admin Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/admin/api-keys` | GET | List all keys |
| `/admin/api-keys/new` | GET/POST | Create a new key |
| `/admin/api-keys/{id}/edit` | GET/POST | View/edit key settings |
| `/admin/api-keys/{id}/revoke` | POST | Revoke a key |
| `/admin/api-keys/{id}/delete` | POST | Permanently delete a key |
| `/admin/api-keys/{id}/rotate` | POST | Rotate key credentials |

## Hooks

| Hook | Arguments | When |
|------|-----------|------|
| `after_api_key_created` | `api_key` | After a new key is created |
| `after_api_key_updated` | `api_key` | After key metadata is updated |
| `after_api_key_revoked` | `api_key` | After a key is revoked |
| `after_api_key_refreshed` | `api_key` | After a key is rotated via refresh token |
| `before_api_key_deleted` | `api_key` | Before a key is permanently deleted |
| `after_api_key_deleted` | `key_id` | After a key is permanently deleted |
