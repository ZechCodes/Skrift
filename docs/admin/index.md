# Admin

Skrift includes an admin interface for managing your site.

## Overview

The admin UI provides:

- User management
- Role assignment
- Site settings configuration
- Page management

## Accessing Admin

The admin interface is available to authenticated users with the admin role.

1. Log in via OAuth
2. Navigate to `/admin`

!!! note "First Admin"
    The first user to complete the setup wizard is automatically assigned the admin role.

## Admin Features

<div class="grid cards" markdown>

-   :material-file-document-edit:{ .lg .middle } **Managing Pages**

    ---

    Create, edit, publish, and delete site pages.

    [:octicons-arrow-right-24: Managing Pages](managing-pages.md)

-   :material-account-group:{ .lg .middle } **User Management**

    ---

    View users and manage role assignments.

    [:octicons-arrow-right-24: User Management](user-management.md)

-   :material-cog:{ .lg .middle } **Site Settings**

    ---

    Configure site name, tagline, and other settings.

-   :material-puzzle:{ .lg .middle } **Custom Admin Pages**

    ---

    Extend the admin interface with your own controllers.

    [:octicons-arrow-right-24: Custom Admin Pages](custom-admin-pages.md)

</div>

## Admin Routes

| Route | Description |
|-------|-------------|
| `/admin` | Admin dashboard |
| `/admin/users` | User list and role management |
| `/admin/pages` | Page management |
| `/admin/settings` | Site settings |

## Roles and Permissions

Skrift uses a role-based authorization system:

| Role | Permissions |
|------|-------------|
| **admin** | Full access (bypasses all permission checks) |
| **editor** | View drafts, manage pages |
| **author** | View drafts |
| **moderator** | View drafts |

### Checking Permissions in Templates

```html
{% if user %}
    <a href="/admin">Admin</a>
{% endif %}
```

Admin routes are protected by authentication guards that verify the user has appropriate roles.
