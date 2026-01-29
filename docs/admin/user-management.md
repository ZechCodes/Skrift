# User Management

Manage user accounts and roles in your Skrift site. Roles and permissions are a key part of Skrift's security model, controlling who can access what.

## User Model

Users are created automatically when they authenticate via OAuth. Each user record contains:

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Unique identifier |
| `oauth_provider` | String | Authentication provider (e.g., "google", "github") |
| `oauth_id` | String | Provider's unique user ID |
| `email` | String | User's email address |
| `name` | String | Display name |
| `picture_url` | String | Profile picture URL |
| `is_active` | Boolean | Account status |
| `last_login_at` | DateTime | Last login timestamp |
| `created_at` | DateTime | Account creation time |
| `updated_at` | DateTime | Last update time |
| `roles` | List | Assigned roles |

## Viewing Users

### Admin UI

Navigate to `/admin/users` to see all registered users and manage their roles.

### Programmatically

```python
from sqlalchemy import select
from skrift.db.models import User

async def list_users(db_session):
    result = await db_session.execute(select(User))
    return result.scalars().all()
```

## User Status

### Active Users

Users with `is_active=True` can log in and access the site.

### Inactive Users

Users with `is_active=False` are blocked from logging in.

To deactivate a user:

```python
async def deactivate_user(db_session, user_id):
    user = await db_session.get(User, user_id)
    if user:
        user.is_active = False
        await db_session.commit()
```

## Roles

Skrift uses role-based authorization. Users can have multiple roles.

### Built-in Roles

| Role | Key Permissions |
|------|-----------------|
| **admin** | Full access (bypasses all permission checks) |
| **editor** | View drafts, manage pages |
| **author** | View drafts |
| **moderator** | View drafts |

### Assigning Roles

```python
from skrift.auth.services import assign_role_to_user

async def make_admin(db_session, user_id):
    await assign_role_to_user(db_session, user_id, "admin")
```

### Removing Roles

```python
from skrift.auth.services import remove_role_from_user

async def revoke_admin(db_session, user_id):
    await remove_role_from_user(db_session, user_id, "admin")
```

### Checking Permissions

```python
from skrift.auth.services import get_user_permissions

async def can_manage_pages(db_session, user_id):
    permissions = await get_user_permissions(db_session, user_id)
    return "manage-pages" in permissions.permissions
```

## Content Visibility

User authentication affects what content is visible:

| User Type | Can View |
|-----------|----------|
| Anonymous | Published pages only |
| Logged in | Published pages only |
| Users with `view-drafts` permission | Published + draft pages |
| Admin | All content + admin UI |

## Session Management

### Session Duration

Sessions last 7 days by default. After expiration, users must re-authenticate.

### Session Data

The session stores:

- `user_id` - UUID of the authenticated user
- `user_name` - Display name
- `user_email` - Email address
- `user_picture_url` - Profile picture URL
- OAuth state tokens (during login flow)

### Logging Out

Users can log out at `/auth/logout`, which clears their session cookie.

## Security Best Practices

### 1. Limit Admin Access

Only grant admin roles to trusted users. The admin role bypasses all permission checks.

### 2. Monitor Login Activity

Track the `last_login_at` field for unusual activity:

```python
from datetime import datetime, timedelta

async def find_inactive_users(db_session, days=90):
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await db_session.execute(
        select(User).where(User.last_login_at < cutoff)
    )
    return result.scalars().all()
```

### 3. Deactivate Unused Accounts

Regularly review and deactivate unused accounts:

```python
from sqlalchemy import update

async def deactivate_inactive_users(db_session, days=180):
    cutoff = datetime.utcnow() - timedelta(days=days)
    await db_session.execute(
        update(User)
        .where(User.last_login_at < cutoff)
        .values(is_active=False)
    )
    await db_session.commit()
```

## See Also

- [Security Model](../core-concepts/security-model.md) - How roles fit into security
- [Protecting Routes](../guides/protecting-routes.md) - Use roles in your controllers
- [OAuth Providers](../reference/auth-providers.md) - Authentication setup
- [Custom Controllers](../guides/custom-controllers.md) - Build admin features
