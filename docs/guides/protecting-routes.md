# Protecting Routes

This guide covers how to protect your routes with authentication and authorization using Skrift's guard system.

## Basic Authentication

To require a user be logged in, use `auth_guard`:

```python
from litestar import get
from skrift.auth.guards import auth_guard

@get("/dashboard", guards=[auth_guard])
async def dashboard():
    return {"message": "Welcome to the dashboard"}
```

If a user isn't logged in, they receive a `401 Not Authorized` response.

## Permission-Based Access

Require specific permissions with the `Permission` class:

```python
from litestar import get
from skrift.auth.guards import auth_guard, Permission

@get("/admin/users", guards=[auth_guard, Permission("manage-users")])
async def list_users():
    return {"users": [...]}
```

The user must have the `manage-users` permission to access this route.

### Built-in Permissions

| Permission | Description | Default Roles |
|------------|-------------|---------------|
| `administrator` | Bypasses all permission checks | admin |
| `manage-users` | User management | admin |
| `manage-pages` | Page management | admin, editor |
| `modify-site` | Site settings | admin |
| `view-drafts` | View unpublished content | admin, editor, author, moderator |

!!! info "Administrator Bypass"
    Users with the `administrator` permission automatically pass all permission checks, even for permissions that don't exist yet.

## Role-Based Access

Require specific roles with the `Role` class:

```python
from litestar import get
from skrift.auth.guards import auth_guard, Role

@get("/editor/queue", guards=[auth_guard, Role("editor")])
async def editor_queue():
    return {"items": [...]}
```

### Built-in Roles

| Role | Permissions |
|------|-------------|
| `admin` | `administrator`, `manage-users`, `manage-pages`, `modify-site` |
| `editor` | `view-drafts`, `manage-pages` |
| `author` | `view-drafts` |
| `moderator` | `view-drafts` |

## Combining Requirements

Requirements can be combined using Python operators.

### AND Logic

Require multiple conditions with `&`:

```python
# User needs BOTH permissions
@get("/publish", guards=[auth_guard, Permission("edit") & Permission("publish")])
async def publish_content():
    ...
```

### OR Logic

Allow alternative conditions with `|`:

```python
# User needs EITHER role
@get("/content", guards=[auth_guard, Role("admin") | Role("editor")])
async def manage_content():
    ...
```

### Complex Combinations

Chain operators for complex logic:

```python
# Admin OR (editor with publish permission)
@get(
    "/approve",
    guards=[
        auth_guard,
        Role("admin") | (Role("editor") & Permission("publish"))
    ]
)
async def approve_content():
    ...
```

## Controller-Level Guards

Apply guards to all routes in a controller:

```python
from litestar import Controller, get
from skrift.auth.guards import auth_guard, Permission

class AdminController(Controller):
    path = "/admin"
    guards = [auth_guard, Permission("administrator")]

    @get("/")
    async def admin_index(self):
        return {"message": "Admin dashboard"}

    @get("/settings")
    async def admin_settings(self):
        return {"settings": {...}}
```

Both routes require the `administrator` permission.

## Route-Specific Overrides

Override controller guards on specific routes:

```python
class ContentController(Controller):
    path = "/content"
    guards = [auth_guard]  # All routes require login

    @get("/")
    async def list_content(self):
        # Uses controller guards - login required
        return {"content": [...]}

    @get("/drafts", guards=[auth_guard, Permission("view-drafts")])
    async def list_drafts(self):
        # Adds permission requirement
        return {"drafts": [...]}
```

## Accessing User Information

Get the current user's ID from the session:

```python
from litestar import get, Request
from skrift.auth.guards import auth_guard

@get("/profile", guards=[auth_guard])
async def profile(request: Request):
    user_id = request.session.get("user_id")
    # Fetch user from database...
    return {"user_id": user_id}
```

## Custom Requirements

Create custom authorization logic by extending `AuthRequirement`:

```python
from skrift.auth.guards import AuthRequirement

class IsOwner(AuthRequirement):
    def __init__(self, resource_type: str):
        self.resource_type = resource_type

    async def check(self, permissions) -> bool:
        # Custom logic here
        # permissions.user_id, permissions.roles, permissions.permissions available
        return True  # or False

# Usage
@get("/posts/{post_id}", guards=[auth_guard, IsOwner("post")])
async def edit_post(post_id: int):
    ...
```

## Error Handling

When authorization fails, Skrift raises `NotAuthorizedException`:

| Scenario | Response |
|----------|----------|
| Not logged in | 401 with "Authentication required" |
| Missing permission/role | 401 with "Insufficient permissions" |

You can customize error handling with Litestar's exception handlers:

```python
from litestar import Litestar
from litestar.exceptions import NotAuthorizedException
from litestar.response import Redirect

def auth_exception_handler(request, exc: NotAuthorizedException):
    return Redirect(path="/auth/login")

app = Litestar(
    exception_handlers={NotAuthorizedException: auth_exception_handler}
)
```

## Best Practices

1. **Always use `auth_guard` first** - It verifies the user is logged in before checking permissions

2. **Prefer permissions over roles** - Permissions are more granular and easier to modify

3. **Use controller-level guards** - Apply common requirements at the controller level to avoid repetition

4. **Keep requirements simple** - Complex permission logic should live in service functions, not guard chains

5. **Test your guards** - Write tests that verify unauthorized users receive 401 responses

## See Also

- [Security Model](../core-concepts/security-model.md) - How Skrift's security works
- [Custom Controllers](custom-controllers.md) - Creating controllers with guards
- [User Management](../admin/user-management.md) - Assigning roles to users
