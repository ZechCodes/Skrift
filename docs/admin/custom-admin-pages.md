# Custom Admin Pages

<span class="skill-badge advanced">:material-star::material-star::material-star: Advanced</span>

Learn how to extend the Skrift admin interface with custom pages and functionality.

## Overview

Skrift's admin interface uses route introspection to automatically build the navigation sidebar. By tagging your routes with `ADMIN_NAV_TAG`, they appear in the admin menu without manual configuration.

## How Admin Navigation Works

The admin sidebar is built dynamically by scanning all registered routes for handlers tagged with `ADMIN_NAV_TAG`. For each matching route:

1. The handler's permission guards are checked against the current user
2. If the user has access, the route appears in the navigation
3. Routes are sorted by their `order` value, then alphabetically by label

This means users only see admin pages they have permission to access.

## Creating a Custom Admin Page

### 1. Create Your Controller

**`controllers/reports.py`**

```python
from litestar import Controller, get, Request
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift import auth_guard, Permission
from skrift.admin import admin_nav, get_admin_context


class ReportsController(Controller):
    """Custom reports admin page."""

    path = "/admin/reports"
    guards = [auth_guard]  # All routes require authentication

    @get(
        "/",
        guards=[auth_guard, Permission("view-reports")],
        **admin_nav("Reports", icon="bar-chart", order=50),
    )
    async def reports_index(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> TemplateResponse:
        """Reports dashboard."""
        ctx = await get_admin_context(request, db_session)

        return TemplateResponse(
            "admin/reports/index.html",
            context={
                "report_data": await self.get_report_data(db_session),
                **ctx,
            },
        )

    async def get_report_data(self, db_session: AsyncSession) -> dict:
        """Fetch report data."""
        # Your report logic here
        return {"total_users": 42, "total_pages": 10}
```

### 2. Create the Template

Templates for admin pages should extend `admin/base.html` to inherit the sidebar layout.

**`templates/admin/reports/index.html`**

```html
{% extends "admin/base.html" %}

{% block title %}Reports - Admin - {{ site_name() }}{% endblock %}

{% block admin_content %}
<hgroup>
    <h1>Reports</h1>
    <p>View site statistics and reports</p>
</hgroup>

<div class="grid">
    <article>
        <header>Total Users</header>
        <p class="stat">{{ report_data.total_users }}</p>
    </article>
    <article>
        <header>Total Pages</header>
        <p class="stat">{{ report_data.total_pages }}</p>
    </article>
</div>
{% endblock %}
```

### 3. Register the Controller

Add your controller to `app.yaml`:

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController
  - skrift.admin.controller:AdminController
  - controllers.reports:ReportsController  # Your custom admin page
```

### 4. Define the Permission (Optional)

If using a custom permission, register it (and optionally a role granting it) at import time — e.g. in your controllers module:

```python
from skrift.auth.permissions import register_permission
from skrift.auth.roles import register_role

register_permission(
    "view-reports",
    display_name="View Reports",
    description="Access the reports dashboard",
)
register_role("analyst", "view-reports", description="View reports and analytics")
```

## Route Configuration Reference

`admin_nav()` produces the route kwargs that register a handler in the sidebar:

```python
from skrift.admin import admin_nav

@get(
    "/admin/custom",
    guards=[auth_guard, Permission("custom-permission")],
    **admin_nav("Custom Page", icon="star", order=50),
)
```

It expands to `tags=[ADMIN_NAV_TAG]` plus an `opt` dict — the long form below is equivalent if you need to set other `opt` keys:

```python
from skrift.admin import ADMIN_NAV_TAG

@get(
    "/admin/custom",
    tags=[ADMIN_NAV_TAG],           # Required for nav inclusion
    guards=[auth_guard, Permission("custom-permission")],
    opt={
        "label": "Custom Page",     # Required: nav menu text
        "icon": "star",             # Optional: Lucide icon (default: "circle")
        "order": 50,                # Optional: sort order (default: 100)
    },
)
```

### Navigation Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `label` | str | Required | Text shown in navigation |
| `icon` | str | `"circle"` | [Lucide icon](https://lucide.dev/icons/) name |
| `order` | int | `100` | Sort priority (lower values appear first) |

### Built-in Admin Page Orders

| Page | Order |
|------|-------|
| Users | 10 |
| Pages | 20 |
| Settings | 100 |

Place your custom pages between these values to control positioning.

## Permission Guards

### Single Permission

```python
@get(
    "/admin/reports",
    tags=[ADMIN_NAV_TAG],
    guards=[auth_guard, Permission("view-reports")],
    opt={"label": "Reports", "order": 30},
)
```

### Multiple Permissions (OR)

Allow access if user has *either* permission:

```python
from skrift.auth.guards import auth_guard, Permission

@get(
    "/admin/content",
    tags=[ADMIN_NAV_TAG],
    guards=[auth_guard, Permission("manage-pages") | Permission("manage-posts")],
    opt={"label": "Content", "order": 25},
)
```

### Multiple Permissions (AND)

Require *both* permissions:

```python
@get(
    "/admin/sensitive",
    tags=[ADMIN_NAV_TAG],
    guards=[auth_guard, Permission("view-reports") & Permission("export-data")],
    opt={"label": "Export", "order": 60},
)
```

### Role-Based Access

```python
from skrift.auth.guards import auth_guard, Role

@get(
    "/admin/editor-tools",
    tags=[ADMIN_NAV_TAG],
    guards=[auth_guard, Role("editor")],
    opt={"label": "Editor Tools", "order": 40},
)
```

## Admin Base Template

The `admin/base.html` template provides:

- Sidebar navigation (automatically populated from `admin_nav` context)
- Active page highlighting
- Consistent admin layout

```html
{% extends "base.html" %}

{% block content %}
<div class="admin-container">
    <aside class="admin-sidebar">
        <nav class="admin-nav">
            <ul>
                {% for item in admin_nav %}
                <li>
                    <a href="{{ item.path }}"
                       {% if current_path == item.path %}class="active"{% endif %}>
                        {{ item.label }}
                    </a>
                </li>
                {% endfor %}
            </ul>
        </nav>
    </aside>
    <div class="admin-content">
        {% block admin_content %}{% endblock %}
    </div>
</div>
{% endblock %}
```

## Getting Admin Context

To include the navigation sidebar, build the admin context with the public helper:

```python
from skrift.admin import get_admin_context

ctx = await get_admin_context(request, db_session)
```

The context includes:

| Key | Type | Description |
|-----|------|-------------|
| `user` | User | Current authenticated user |
| `permissions` | UserPermissions | User's permissions and roles |
| `admin_nav` | list[AdminNavItem] | Filtered navigation items |
| `current_path` | str | Current request path |

## Complete Example

Here's a full example of a custom admin page for managing site analytics:

**`controllers/analytics.py`**

```python
from litestar import Controller, get, Request
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift import auth_guard, Permission
from skrift.admin import admin_nav, get_admin_context


class AnalyticsController(Controller):
    """Analytics admin pages."""

    path = "/admin/analytics"
    guards = [auth_guard]

    @get(
        "/",
        guards=[auth_guard, Permission("view-analytics")],
        **admin_nav("Analytics", icon="activity", order=35),
    )
    async def dashboard(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> TemplateResponse:
        """Analytics dashboard."""
        ctx = await get_admin_context(request, db_session)

        return TemplateResponse(
            "admin/analytics/dashboard.html",
            context={
                "page_views": 1234,
                "unique_visitors": 567,
                **ctx,
            },
        )

    @get(
        "/export",
        guards=[auth_guard, Permission("export-analytics")],
    )
    async def export(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> dict:
        """Export analytics data (no nav entry)."""
        # This route doesn't have ADMIN_NAV_TAG, so it won't
        # appear in navigation but is still accessible
        return {"data": [...]}
```

## Next Steps

- [Custom Controllers](../guides/custom-controllers.md) - General controller guide
- [Protecting Routes](../guides/protecting-routes.md) - Complete auth guard reference
- [User Management](user-management.md) - Managing users and roles
