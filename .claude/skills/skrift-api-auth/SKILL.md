---
name: skrift-api-auth
description: "Skrift API key authentication — bearer tokens, permission scoping, key rotation via refresh tokens, admin management, and route guard markers."
---

# Skrift API Key Authentication

Programmatic authentication via bearer tokens (`sk_...`) with per-key permission/role scoping and refresh-token-based key rotation.

## Key Format

- API key: `sk_<secrets.token_urlsafe(32)>` — stored as SHA-256 hash
- Refresh token: `skr_<secrets.token_urlsafe(32)>` — stored as SHA-256 hash
- Raw values shown **once** at creation; only `key_prefix` (first 12 chars) and hashes are persisted

## Configuration

```yaml
api_keys:
  enabled: true
  default_expiration_days: 365
  max_keys_per_user: 10
  refresh_token_expiration_days: 30
```

`APIKeyConfig` in `skrift/config.py`, accessed via `settings.api_keys`.

## Model

`skrift/db/models/api_key.py` — `APIKey` extends `Base`:

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | FK → users.id | Owner |
| `display_name` | String(255) | Label |
| `description` | Text, nullable | Notes |
| `key_prefix` | String(12) | For display |
| `key_hash` | String(128), unique, indexed | SHA-256 lookup |
| `scoped_permissions` | Text, nullable | Newline-delimited |
| `scoped_roles` | Text, nullable | Newline-delimited |
| `is_active` | Boolean | Active toggle |
| `expires_at` | DateTime, nullable | Key expiry |
| `last_used_at` | DateTime, nullable | Usage tracking |
| `last_used_ip` | String(45), nullable | Client IP |
| `refresh_token_hash` | String(128), nullable, unique | Refresh token hash |
| `refresh_token_expires_at` | DateTime, nullable | Refresh expiry |

Helper properties: `scoped_permission_list`, `scoped_role_list`, `is_expired`, `refresh_token_expired`.

User relationship: `User.api_keys` (one-to-many, cascade delete).

## Service Layer

`skrift/db/services/api_key_service.py` — async functions:

```python
create_api_key(session, user_id, display_name, ...) -> (APIKey, raw_key, raw_refresh)
verify_api_key(session, raw_key, client_ip=None) -> APIKey | None
refresh_api_key(session, raw_refresh_token, ...) -> (APIKey, new_key, new_refresh) | None
list_api_keys(session, user_id=None) -> list[APIKey]
get_api_key(session, key_id) -> APIKey | None
update_api_key(session, api_key, **kwargs) -> APIKey
revoke_api_key(session, key_id) -> None  # is_active=False
delete_api_key(session, key_id) -> None
```

`verify_api_key` checks: exists, `is_active`, not expired, `user.is_active`. Updates `last_used_at`/`last_used_ip`.

`refresh_api_key` atomically replaces key + refresh token. Old credentials stop working immediately.

## Guards

`skrift/auth/guards.py`:

```python
from skrift.auth.guards import auth_guard, APIKeyAuth, APIKeyOnly, Permission

# Session only (unchanged default)
@get("/admin/users", guards=[auth_guard, Permission("manage-users")])

# API-compatible (session OR API key)
@get("/api/pages", guards=[auth_guard, APIKeyAuth(), Permission("manage-pages")])

# API-only (rejects session auth)
@post("/api/v1/sync", guards=[auth_guard, APIKeyOnly(), Permission("manage-pages")])
```

`auth_guard` detects `APIKeyAuth`/`APIKeyOnly` markers in the route's guards list and:
1. Extracts `Authorization: Bearer sk_...` header
2. Calls `verify_api_key()` to validate
3. Computes scoped permissions (intersection of user permissions and key scope)
4. Falls back to session auth if no API key and route isn't API-only

Permission scoping: if key has `scoped_permissions`/`scoped_roles`, effective permissions = `(scoped ∩ user_actual)`. If both are null, key inherits all user permissions.

## Refresh Endpoint

`skrift/controllers/api_auth.py` — `APIAuthController`:

```
POST /api/auth/refresh
Body: {"refresh_token": "skr_..."}
Response: {"key": "sk_...", "refresh_token": "skr_...", "key_prefix": "...", "expires_at": "...", "refresh_token_expires_at": "..."}
```

CSRF-exempt. No guards (refresh token is the credential).

## Admin Controller

`skrift/admin/api_keys.py` — `APIKeyAdminController`:

| Route | Method | Purpose |
|-------|--------|---------|
| `/admin/api-keys` | GET | List (nav: "API Keys", icon: "key", order: 91) |
| `/admin/api-keys/new` | GET/POST | Create — raw key/refresh stored in session for show-once |
| `/admin/api-keys/{id}/edit` | GET/POST | Edit metadata |
| `/admin/api-keys/{id}/revoke` | POST | Set is_active=False |
| `/admin/api-keys/{id}/delete` | POST | Hard delete |
| `/admin/api-keys/{id}/rotate` | POST | Manual key rotation |

All routes require `Permission("manage-api-keys")` (included in admin role).

Templates: `skrift/templates/admin/api_keys/list.html`, `edit.html`.

## Hooks

`after_api_key_created`, `after_api_key_updated`, `after_api_key_revoked`, `after_api_key_refreshed`, `before_api_key_deleted`, `after_api_key_deleted`.

## Key Files

| File | Purpose |
|------|---------|
| `skrift/db/models/api_key.py` | APIKey model |
| `skrift/db/services/api_key_service.py` | Key CRUD, verify, refresh |
| `skrift/auth/guards.py` | `APIKeyAuth`, `APIKeyOnly`, dual-auth `auth_guard` |
| `skrift/controllers/api_auth.py` | Refresh endpoint |
| `skrift/admin/api_keys.py` | Admin controller |
| `skrift/config.py` | `APIKeyConfig` |
| `skrift/auth/roles.py` | `manage-api-keys` permission on admin role |
