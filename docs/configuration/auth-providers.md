# OAuth Providers

Skrift supports multiple OAuth providers for authentication. The setup wizard configures these automatically, but you can also edit `app.yaml` directly.

## Supported Providers

- Google
- GitHub
- Microsoft
- Discord
- Facebook
- Twitter/X

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

## Authentication Flow

1. User clicks login and selects a provider
2. Skrift redirects to the provider's consent screen
3. User approves and provider redirects back with an auth code
4. Skrift exchanges the code for tokens and fetches user info
5. User record is created/updated and session is established

## Routes

Each provider gets these routes automatically:

| Route | Description |
|-------|-------------|
| `/auth/{provider}/login` | Initiate OAuth flow |
| `/auth/{provider}/callback` | Handle OAuth callback |
| `/auth/logout` | Clear session |

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
