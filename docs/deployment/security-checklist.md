# Security Checklist

Use this checklist before deploying Skrift to production. Each item addresses a specific security concern.

## Required

These items must be completed for a secure deployment.

### Secret Key

- [ ] `SECRET_KEY` is set to a cryptographically random value
- [ ] `SECRET_KEY` is NOT `dev-secret-key` or any default value
- [ ] `SECRET_KEY` is stored in environment variables, not in `app.yaml`

Generate a secure key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

!!! danger "Critical"
    A weak or default secret key allows attackers to forge session cookies and impersonate any user, including administrators.

### No Dummy Authentication

- [ ] `dummy` is NOT in your `app.yaml` providers list
- [ ] `SKRIFT_ENV` is unset or set to `production`

Skrift will refuse to start if dummy auth is configured in production, but verify your configuration is correct:

```yaml
# WRONG - dummy auth in production config
auth:
  providers:
    dummy: {}  # Remove this!
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

```yaml
# CORRECT - only real OAuth providers
auth:
  providers:
    google:
      client_id: $GOOGLE_CLIENT_ID
      client_secret: $GOOGLE_CLIENT_SECRET
```

### HTTPS

- [ ] Your domain has a valid SSL/TLS certificate
- [ ] All traffic is served over HTTPS
- [ ] HTTP requests redirect to HTTPS

Without HTTPS:

- Session cookies can be intercepted (even though they're encrypted)
- OAuth tokens can be stolen in transit
- Users see browser security warnings

### OAuth Configuration

- [ ] OAuth redirect URLs point to your production domain (not localhost)
- [ ] OAuth client secrets are stored in environment variables
- [ ] You've registered your production callback URLs with each provider

Example Google callback URL for production:
```
https://yourdomain.com/auth/google/callback
```

### Database Credentials

- [ ] Database URL uses environment variables
- [ ] Database user has minimum required permissions
- [ ] Database is not publicly accessible

```yaml
# CORRECT - credentials in environment
db:
  url: $DATABASE_URL

# WRONG - hardcoded credentials
db:
  url: postgresql://admin:password123@db.example.com/skrift
```

## Recommended

These items significantly improve security.

### Environment Separation

- [ ] Production uses `app.yaml` (not `app.dev.yaml` or `app.staging.yaml`)
- [ ] Development credentials are not in production config
- [ ] `SKRIFT_ENV` is explicitly set in your deployment configuration

### Database Security

- [ ] Using PostgreSQL (not SQLite) for production
- [ ] Database connections use SSL
- [ ] Regular database backups are configured

### Monitoring

- [ ] Application logs are being collected
- [ ] Failed login attempts are monitored
- [ ] Error alerting is configured

### Headers

Skrift automatically injects security response headers (CSP, HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, Cross-Origin-Opener-Policy) on every response. The `Server` header is also suppressed.

- [ ] Review default `security_headers` config — customize CSP in `app.yaml` if your site loads external scripts, fonts, or styles
- [ ] If using a reverse proxy, its headers are additive — Skrift won't overwrite headers already set by the proxy

```yaml
# app.yaml - customize if needed
security_headers:
  content_security_policy: "default-src 'self'; script-src 'self' https://cdn.example.com"
```

If you still want to add headers at the reverse proxy level (e.g., additional nginx-specific headers):

```nginx
# nginx example - these are optional since Skrift handles the core set
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

## Verification Commands

Run these commands to verify your configuration:

### Check Environment

```bash
# Should output "production" or be empty
echo $SKRIFT_ENV

# Should NOT be a dev key
echo $SECRET_KEY | head -c 20
```

### Check Config File

```bash
# Should NOT contain "dummy"
grep -r "dummy" app.yaml

# Should use environment variables for secrets
grep -E '\$[A-Z_]+' app.yaml
```

### Test OAuth Callbacks

After deployment, verify OAuth works:

1. Visit your production site
2. Click login
3. Complete OAuth flow
4. Verify you're redirected back to your production domain

### Check Session Cookies

Using browser developer tools:

1. Log in to your site
2. Open Developer Tools > Application > Cookies
3. Verify the session cookie has:
   - `HttpOnly`: Yes
   - `Secure`: Yes
   - `SameSite`: Lax

## Common Issues

### "OAuth redirect_uri_mismatch"

The callback URL in your OAuth provider settings doesn't match `redirect_base_url` in `app.yaml`.

**Fix:** Update `app.yaml`:
```yaml
auth:
  redirect_base_url: https://yourdomain.com  # Must match OAuth settings
```

### "Invalid OAuth state"

CSRF protection is blocking the request, usually because:

- Session expired during OAuth flow
- User opened multiple login tabs
- Attacker attempted CSRF

**Fix:** This is working correctly. User should try logging in again.

### Server Won't Start - Dummy Auth Error

```
SECURITY ERROR: Dummy auth provider is configured in production.
```

**Fix:** Remove `dummy` from `auth.providers` in your `app.yaml`.

## See Also

- [Security Model](../core-concepts/security-model.md) - How Skrift's security works
- [Production Deployment](production.md) - Full deployment guide
- [Environment Variables](../reference/environment-variables.md) - All configuration options
