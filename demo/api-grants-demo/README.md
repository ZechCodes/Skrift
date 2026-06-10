# Skrift API Grants Demo

This demo runs two Skrift sites:

- **Provider**: `http://localhost:8091`
  Owns protected API endpoints and exposes the API permission grant flow.
- **Client**: `http://localhost:8092`
  Provides a form for requesting each provider permission with and without an existing service API key.

## Run

```bash
cd demo/api-grants-demo
docker compose up --build
```

Then open:

```text
http://localhost:8092
```

The provider uses dummy auth. When the grant flow redirects you to the provider login page, sign in with any email address.

## Seeded Service Key

The provider bootstrap creates a known service API key:

```text
sk_demo_known_service_key
```

That key has:

- `demo-known-read`
- `demo-elevated-write`

It does not have:

- `demo-anonymous-read`
- `demo-disallowed-admin`

The client form is prefilled with this key so the successful and failing paths are easy to compare.

## Permission Cases

| Permission | Clearance | Expected without key | Expected with seeded key |
|------------|-----------|----------------------|--------------------------|
| `demo-anonymous-read` | `allow-anonymous-service` | Consent, token exchange, API call succeeds | Consent, token exchange, API call succeeds; the key is not required |
| `demo-known-read` | `require-known-service` | Error page, no consent screen | Consent, token exchange, API call succeeds |
| `demo-elevated-write` | `require-elevated-security` | Error page, no consent screen | Consent, token exchange, API call succeeds |
| `demo-disallowed-admin` | `disallow-api-grants` | Error page, no consent screen | Request rejected |

## Discovery

The provider advertises anonymous grant support at:

```text
http://localhost:8091/.well-known/skrift
```

Only `allow-anonymous-service` permissions are listed in discovery.
