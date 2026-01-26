# Custom Controllers

<span class="skill-badge advanced">:material-star::material-star::material-star: Advanced</span>

Learn how to extend Skrift with custom controllers for new routes and functionality.

## Overview

Controllers in Skrift are Litestar controller classes that handle HTTP requests. They're loaded dynamically from `app.yaml`, allowing you to add functionality without modifying core code.

## Creating a Controller

### 1. Create the Controller File

**`controllers/api.py`**

```python
from litestar import Controller, get, post
from litestar.response import Template as TemplateResponse
from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

class ApiController(Controller):
    path = "/api"

    @get("/health")
    async def health_check(self) -> dict:
        """Health check endpoint."""
        return {"status": "healthy"}

    @get("/pages")
    async def list_pages(
        self,
        db_session: AsyncSession
    ) -> list[dict]:
        """List all published pages."""
        from skrift.db.services import page_service

        pages = await page_service.list_pages(
            db_session,
            published_only=True
        )
        return [
            {"slug": p.slug, "title": p.title}
            for p in pages
        ]
```

### 2. Register in app.yaml

```yaml
controllers:
  - skrift.controllers.auth:AuthController
  - skrift.controllers.web:WebController
  - controllers.api:ApiController  # Your custom controller
```

### 3. Restart the Application

```bash
python -m skrift
```

Your endpoints are now available:

- `GET /api/health`
- `GET /api/pages`

## Controller Patterns

### Template Response

Return HTML using Jinja2 templates:

```python
from litestar.response import Template as TemplateResponse

class BlogController(Controller):
    path = "/blog"

    @get("/")
    async def index(
        self,
        request: Request,
        db_session: AsyncSession
    ) -> TemplateResponse:
        posts = await get_posts(db_session)
        return TemplateResponse(
            "blog/index.html",
            context={"posts": posts}
        )
```

### Request Data

Handle POST data with Pydantic models:

```python
from pydantic import BaseModel

class ContactForm(BaseModel):
    name: str
    email: str
    message: str

class ContactController(Controller):
    path = "/contact"

    @post("/")
    async def submit(
        self,
        data: ContactForm,
        db_session: AsyncSession
    ) -> dict:
        # Process the form
        await save_contact(db_session, data)
        return {"success": True}
```

### Authentication Check

Access the current user from the session:

```python
from skrift.db.models import User

class DashboardController(Controller):
    path = "/dashboard"

    @get("/")
    async def index(
        self,
        request: Request,
        db_session: AsyncSession
    ) -> TemplateResponse:
        user_id = request.session.get("user_id")

        if not user_id:
            from litestar.response import Redirect
            return Redirect("/auth/login")

        user = await get_user(db_session, user_id)
        return TemplateResponse(
            "dashboard/index.html",
            context={"user": user}
        )
```

### Path Parameters

Capture URL segments:

```python
from uuid import UUID

class PageController(Controller):
    path = "/pages"

    @get("/{page_id:uuid}")
    async def get_page(
        self,
        page_id: UUID,
        db_session: AsyncSession
    ) -> dict:
        page = await get_page_by_id(db_session, page_id)
        if not page:
            from litestar.exceptions import NotFoundException
            raise NotFoundException("Page not found")
        return {"id": str(page.id), "title": page.title}
```

### Query Parameters

Handle URL query strings:

```python
@get("/search")
async def search(
    self,
    q: str,
    limit: int = 10,
    db_session: AsyncSession
) -> list[dict]:
    results = await search_pages(db_session, q, limit)
    return [{"slug": r.slug, "title": r.title} for r in results]
```

Usage: `GET /api/search?q=python&limit=5`

## Dependency Injection

Litestar automatically injects these dependencies:

| Parameter | Type | Description |
|-----------|------|-------------|
| `request` | Request | The HTTP request object |
| `db_session` | AsyncSession | Database session |

### Custom Dependencies

Register custom dependencies in `asgi.py`:

```python
from litestar import Litestar
from litestar.di import Provide

async def get_settings():
    from skrift.config import get_settings
    return get_settings()

app = Litestar(
    dependencies={"settings": Provide(get_settings)}
)
```

Use in controllers:

```python
@get("/config")
async def get_config(self, settings: Settings) -> dict:
    return {"debug": settings.debug}
```

## Error Handling

### HTTP Exceptions

```python
from litestar.exceptions import (
    NotFoundException,
    NotAuthorizedException,
    ValidationException
)

@get("/{slug:str}")
async def get_page(self, slug: str, db_session: AsyncSession):
    page = await page_service.get_page_by_slug(db_session, slug)

    if not page:
        raise NotFoundException(f"Page '{slug}' not found")

    if not page.is_published:
        raise NotAuthorizedException("Page not published")

    return page
```

### Custom Error Responses

```python
from litestar.response import Response

@post("/upload")
async def upload(self, request: Request) -> Response:
    try:
        await process_upload(request)
        return Response({"success": True}, status_code=201)
    except FileTooLargeError:
        return Response(
            {"error": "File too large"},
            status_code=413
        )
```

## Using the Template Class

Leverage Skrift's WordPress-like template resolution:

```python
from skrift.lib.template import Template

class CustomPageController(Controller):
    path = "/custom"

    @get("/{path:path}")
    async def view(
        self,
        path: str,
        request: Request,
        db_session: AsyncSession
    ) -> TemplateResponse:
        slugs = path.split("/")

        # Uses template hierarchy: custom-a-b.html → custom-a.html → custom.html
        template = Template("custom", *slugs)

        return template.render(
            "templates",  # Template directory
            path=path,
            slugs=slugs,
            user=await get_current_user(request, db_session)
        )
```

## Testing Controllers

```python
import pytest
from litestar.testing import TestClient
from skrift.asgi import create_app

@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)

def test_health_check(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

def test_list_pages(client):
    response = client.get("/api/pages")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
```

## Best Practices

### 1. Organize by Feature

```
controllers/
├── api/
│   ├── __init__.py
│   ├── pages.py
│   └── users.py
├── admin/
│   ├── __init__.py
│   └── dashboard.py
└── public/
    └── blog.py
```

### 2. Use Type Hints

```python
from typing import Optional
from uuid import UUID

@get("/pages/{page_id:uuid}")
async def get_page(
    self,
    page_id: UUID,
    include_drafts: Optional[bool] = False,
    db_session: AsyncSession
) -> dict:
    ...
```

### 3. Validate Input

```python
from pydantic import BaseModel, EmailStr, Field

class CreateUser(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)

@post("/users")
async def create_user(self, data: CreateUser) -> dict:
    ...
```

### 4. Document Endpoints

```python
@get(
    "/pages",
    summary="List all pages",
    description="Returns a list of all published pages",
    tags=["Pages"]
)
async def list_pages(self) -> list[dict]:
    ...
```

## Next Steps

- [Litestar Documentation](https://litestar.dev/) - Full framework reference
- [Configuration](../configuration/index.md) - Controller registration
- [Deployment](../deployment/production.md) - Production setup
