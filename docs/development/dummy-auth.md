# Dummy Authentication

The dummy authentication provider lets you test user flows without configuring OAuth providers. It's useful for:

- Quick prototyping without OAuth setup
- Testing different user accounts
- CI/CD pipelines
- Offline development

## Enabling Dummy Auth

Add the dummy provider to your `app.dev.yaml`:

```yaml
# app.dev.yaml (NOT app.yaml!)
auth:
  redirect_base_url: http://localhost:8080
  providers:
    dummy: {}  # No credentials needed
```

Then run with the dev environment:

```bash
export SKRIFT_ENV=dev
python -m skrift
```

## Using Dummy Auth

1. Visit your site and click "Login"
2. Click "Dummy Login (Dev)" on the provider selection page
3. Enter any email address and optional name
4. Click "Log In"

You're now logged in as a user with that email address.

### Consistent User Identity

The same email always creates/logs in as the same user:

- `alice@example.com` is always the same user
- `bob@test.local` is always the same user
- User IDs are deterministic based on email

This makes testing predictable—you can create test users and always log back in as them.

## Testing Different Roles

To test role-based features:

1. Log in as any user via dummy auth
2. Use the admin panel to assign roles to that user
3. Log out and back in to test the role's permissions

Or create multiple test accounts:

```
admin@test.local    -> Assign admin role
editor@test.local   -> Assign editor role
viewer@test.local   -> Keep as basic user
```

## Production Safety

!!! danger "Production Kill Switch"
    Dummy auth is **blocked from production**. If you accidentally configure it in `app.yaml` and deploy, the server will:

    1. Print a security error message
    2. Terminate the worker process
    3. Kill the parent uvicorn process to prevent respawning
    4. Exit with code 1

This isn't a warning you can ignore—the server physically cannot start.

### How It Works

When Skrift starts, it checks if:

1. `SKRIFT_ENV` is unset or `production`
2. `dummy` is in `app.yaml`'s auth providers

If both are true, the server terminates immediately:

```
======================================================================
SECURITY ERROR: Dummy auth provider is configured in production.
Remove 'dummy' from auth.providers in app.yaml.
Server will NOT start.
======================================================================
```

### Why So Aggressive?

Dummy auth allows anyone to log in as any user by simply entering an email address. If this reached production:

- Attackers could log in as any existing user
- Attackers could create admin accounts
- All authentication would be meaningless

The kill switch ensures this can never happen, even if someone makes a deployment mistake.

## Development Workflow

A typical workflow using dummy auth:

```bash
# 1. Set up dev environment
export SKRIFT_ENV=dev
export SECRET_KEY=dev-secret-key

# 2. Start Skrift
python -m skrift

# 3. Complete setup wizard, choosing dummy auth

# 4. Log in as your first user (becomes admin)
# Use: admin@localhost

# 5. Create test content, test features

# 6. Test as non-admin user
# Log out, log in as: user@localhost

# 7. When ready for production, configure real OAuth
# Edit app.yaml (not app.dev.yaml) with Google/GitHub/etc.
```

## Identifying Dummy Users

Users created via dummy auth have `oauth_provider: dummy` in the database. You can identify them with:

```python
# In your code
if user.oauth_provider == "dummy":
    print("This is a test user")
```

Or via SQL:

```sql
SELECT * FROM users WHERE oauth_provider = 'dummy';
```

## Limitations

Dummy auth is intentionally limited:

| Feature | Real OAuth | Dummy Auth |
|---------|------------|------------|
| Email verification | Provider verifies | None |
| Profile pictures | From provider | None |
| Token refresh | Automatic | N/A |
| Account linking | Supported | N/A |
| Production use | Yes | No |

## See Also

- [Security Model](../core-concepts/security-model.md) - How Skrift protects against misconfigurations
- [OAuth Providers](../reference/auth-providers.md) - Setting up real authentication
- [Security Checklist](../deployment/security-checklist.md) - Production deployment verification
