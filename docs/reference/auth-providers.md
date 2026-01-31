# OAuth Providers

Skrift supports multiple OAuth providers for authentication. The setup wizard configures these automatically, but you can also edit `app.yaml` directly.

## Supported Providers

- Google
- GitHub
- Microsoft
- Discord
- Facebook
- Twitter/X
- **Dummy** (development/testing only)

## Configuration

OAuth providers are configured in `app.yaml` under the `auth` section:

```yaml
auth:
  redirect_base_url: http://localhost:8080
  providers:
    google:
      client_id: your-client-id.apps.googleusercontent.com
      client_secret: your-client-secret
      scopes:
        - openid
        - email
        - profile
```

### Using Environment Variables

To keep secrets out of your config file, reference environment variables with the `$` prefix:

```yaml
auth:
  redirect_base_url: http://localhost:8080
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

This way you can safely commit `app.yaml` to version control while keeping credentials in your environment:

```bash
export GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
export GOOGLE_CLIENT_SECRET=your-client-secret
```

## Provider Setup

### Google

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and go to **APIs & Services** > **Credentials**
3. Click **Create Credentials** > **OAuth client ID**
4. Select **Web application**
5. Add authorized redirect URI: `http://localhost:8080/auth/google/callback`
6. Copy the Client ID and Client Secret

```yaml
providers:
  google:
    client_id: your-client-id.apps.googleusercontent.com
    client_secret: your-client-secret
```

### GitHub

1. Go to [GitHub Developer Settings](https://github.com/settings/developers)
2. Click **New OAuth App**
3. Set Authorization callback URL: `http://localhost:8080/auth/github/callback`
4. Copy the Client ID and Client Secret

```yaml
providers:
  github:
    client_id: your-client-id
    client_secret: your-client-secret
```

### Microsoft

1. Go to [Azure Portal](https://portal.azure.com/) > **App registrations**
2. Click **New registration**
3. Add redirect URI: `http://localhost:8080/auth/microsoft/callback`
4. Create a client secret under **Certificates & secrets**

```yaml
providers:
  microsoft:
    client_id: your-client-id
    client_secret: your-client-secret
    tenant_id: common  # or your tenant ID
```

### Discord

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create an application and go to **OAuth2**
3. Add redirect: `http://localhost:8080/auth/discord/callback`
4. Copy the Client ID and Client Secret

```yaml
providers:
  discord:
    client_id: your-client-id
    client_secret: your-client-secret
```

### Dummy Provider (Development Only)

The dummy provider allows you to bypass OAuth flows during development and testing. Instead of redirecting to an external provider, it shows a simple form where you can enter any email and name to log in.

!!! warning "Not for Production"
    The dummy provider will **refuse to start** if enabled in production. This is a critical security measure to prevent accidental deployment with insecure authentication.

**Configuration:**

```yaml
# In app.dev.yaml (NOT app.yaml for production)
auth:
  redirect_base_url: http://localhost:8080
  providers:
    dummy: {}  # No credentials needed
```

**Features:**

- No OAuth credentials required
- Enter any email/name to create or log in as a user
- Same email always logs in as the same user (deterministic)
- Useful for testing user flows, roles, and permissions
- Cannot be enabled in production (server refuses to start)

**Usage:**

1. Add `dummy: {}` to your `app.dev.yaml` providers
2. Run with `SKRIFT_ENV=dev`
3. Click "Dummy Login (Dev)" on the login page
4. Enter an email and optional name
5. You're logged in!

**Security:**

- The server will immediately exit with an error if dummy auth is detected in production (`SKRIFT_ENV=production` or unset)
- The dummy provider does not appear in the setup wizard
- Users created via dummy login have `oauth_provider: dummy` for easy identification

## Authentication Flow

1. User clicks login and selects a provider
2. Skrift redirects to the provider's consent screen
3. User approves and provider redirects back with an auth code
4. Skrift exchanges the code for tokens and fetches user info
5. User record is created/updated and session is established

## Post-Login Redirects

By default, users are redirected to `/` (home page) after successful login. You can customize this behavior using the `next` query parameter to redirect users back to their intended destination.

### Using the `next` Parameter

Add a `next` query parameter to the login URL:

```
/auth/login?next=/dashboard
/auth/google/login?next=/settings/profile
```

After successful authentication, the user will be redirected to the specified URL instead of the home page.

### Security

Redirect URLs are validated to prevent open redirect vulnerabilities:

- **Relative paths** starting with `/` are always allowed (e.g., `/dashboard`, `/settings`)
- **Protocol-relative URLs** like `//evil.com` are blocked
- **Absolute URLs** require the domain to be in `allowed_redirect_domains`

### Cross-Domain Redirects

To allow redirects to external domains (e.g., subdomains or related applications), configure `allowed_redirect_domains`:

```yaml
auth:
  redirect_base_url: https://auth.example.com
  allowed_redirect_domains:
    - example.com           # Matches example.com and *.example.com
    - "*.myapp.io"          # Wildcard: any subdomain of myapp.io
    - "app-*.example.com"   # Wildcard: app-dev, app-staging, etc.
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

**Pattern matching:**

| Pattern | Matches |
|---------|---------|
| `example.com` | `example.com`, `app.example.com`, `api.example.com` |
| `*.example.com` | `app.example.com`, `api.example.com` (not `example.com` itself) |
| `app-*.example.com` | `app-dev.example.com`, `app-staging.example.com` |
| `*-staging.example.com` | `api-staging.example.com`, `web-staging.example.com` |

**Examples:**

```
# Relative path - always works
/auth/login?next=/dashboard

# Same domain - always works
/auth/login?next=https://auth.example.com/profile

# Subdomain - works if example.com is in allowed_redirect_domains
/auth/login?next=https://app.example.com/dashboard

# External domain - blocked unless explicitly allowed
/auth/login?next=https://evil.com  → redirects to / instead
```

!!! warning "Security Note"
    Only add domains you trust to `allowed_redirect_domains`. Allowing external redirects can enable phishing attacks if misconfigured.

## Account Linking

Skrift automatically links OAuth accounts that share the same email address. This means a user can log in with multiple providers (e.g., GitHub and Discord) and access the same account, as long as both providers return the same email.

### How It Works

When a user authenticates, Skrift follows this logic:

1. **Existing OAuth account?** If the user has logged in with this exact provider before (matched by provider + provider account ID), log them in.

2. **Email matches existing user?** If no OAuth account exists for this provider but a user with the same email already exists, link the new OAuth account to that existing user.

3. **New user?** If neither condition is met, create a new user and OAuth account.

### Example

1. Alice signs up using GitHub with `alice@example.com`
2. Later, Alice clicks "Login with Discord" using the same email
3. Skrift sees no Discord OAuth account exists, but finds a user with `alice@example.com`
4. The Discord OAuth account is linked to Alice's existing user record
5. Alice can now log in with either GitHub or Discord

### Requirements

- The email address must be **verified** by the OAuth provider (Skrift requests the `email` scope)
- The email must match **exactly** (case-insensitive comparison)
- Each OAuth provider account can only be linked to one user

### Viewing Linked Accounts

Users can see which providers are linked to their account in their profile settings. Each linked provider shows the email address associated with that OAuth account.

## Provider Metadata

Skrift stores the full raw response from each OAuth provider in the `provider_metadata` field. This allows you to access provider-specific data like usernames, avatars, and other profile information.

### Available Fields by Provider

| Provider | Key Fields |
|----------|-----------|
| Discord | `id`, `username`, `global_name`, `discriminator`, `avatar`, `email`, `verified`, `locale` |
| GitHub | `id`, `login`, `name`, `email`, `avatar_url`, `bio`, `company`, `location`, `public_repos` |
| Google | `id`, `email`, `name`, `picture`, `verified_email`, `locale`, `hd` |
| Twitter | `id`, `username`, `name` |
| Microsoft | `id`, `displayName`, `mail`, `userPrincipalName` |
| Facebook | `id`, `name`, `email`, `picture.data.url` |

### Accessing Metadata

Use the service functions in `skrift.db.services.oauth_service`:

```python
from skrift.db.services.oauth_service import (
    get_provider_metadata,
    get_provider_username,
    get_provider_avatar_url,
)

# Get raw metadata dict
metadata = await get_provider_metadata(db_session, user_id, "discord")
# Returns: {"id": "123", "username": "alice", "avatar": "abc123", ...}

# Get provider-specific username
username = await get_provider_username(db_session, user_id, "github")
# Returns: "alice" (from the 'login' field for GitHub)

# Get avatar URL (handles provider-specific construction)
avatar = await get_provider_avatar_url(db_session, user_id, "discord")
# Returns: "https://cdn.discordapp.com/avatars/123/abc123.png"
```

### Helper Functions

```python
from skrift.db.services.oauth_service import extract_metadata_field

# Safely extract nested fields
metadata = {"picture": {"data": {"url": "https://..."}}}
url = extract_metadata_field(metadata, "picture", "data", "url")
# Returns: "https://..."

# With default value
value = extract_metadata_field(metadata, "missing", "field", default="N/A")
# Returns: "N/A"
```

## Routes

Each provider gets these routes automatically:

| Route | Description |
|-------|-------------|
| `/auth/login` | Login page with all providers |
| `/auth/{provider}/login` | Initiate OAuth flow for specific provider |
| `/auth/{provider}/callback` | Handle OAuth callback |
| `/auth/logout` | Clear session |

The login routes accept an optional `?next=` query parameter to redirect users after successful authentication. See [Post-Login Redirects](#post-login-redirects) for details.

## Troubleshooting

### "redirect_uri_mismatch"

The callback URL doesn't match what's registered with the provider.

**Fix:** Ensure `redirect_base_url` in `app.yaml` matches exactly what you registered, and that you've added the full callback URL (e.g., `http://localhost:8080/auth/google/callback`).

### "invalid_client"

Client ID or secret is wrong.

**Fix:** Double-check the values in `app.yaml`. If using env vars, verify they're set correctly.

### "access_denied"

User denied permission or app isn't verified.

**Fix:** For Google, add test users in the OAuth consent screen settings during development.
