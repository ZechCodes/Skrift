# Skrift Code Patterns

## Controller Patterns

### Basic Controller

```python
from pathlib import Path
from litestar import Controller, get, post
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

class ItemController(Controller):
    path = "/items"

    @get("/")
    async def list_items(self, db_session: AsyncSession) -> TemplateResponse:
        items = await item_service.list_items(db_session)
        return TemplateResponse("items/list.html", context={"items": items})

    @get("/{item_id:uuid}")
    async def get_item(
        self, db_session: AsyncSession, item_id: UUID
    ) -> TemplateResponse:
        item = await item_service.get_by_id(db_session, item_id)
        if not item:
            raise NotFoundException(f"Item {item_id} not found")
        return TemplateResponse("items/detail.html", context={"item": item})
```

### Protected Controller with Guards

```python
from skrift.auth import auth_guard, Permission, Role

class AdminController(Controller):
    path = "/admin"
    guards = [auth_guard, Role("admin")]

    @get("/")
    async def dashboard(self) -> TemplateResponse:
        return TemplateResponse("admin/dashboard.html")

    @get("/users")
    async def list_users(self, db_session: AsyncSession) -> TemplateResponse:
        # Additional permission check beyond role
        ...
```

### Controller with Request/Session Access

```python
from litestar import Request

class AuthController(Controller):
    path = "/auth"

    @get("/profile")
    async def profile(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        user_id = request.session.get("user_id")
        if not user_id:
            raise NotAuthorizedException()

        user = await user_service.get_by_id(db_session, UUID(user_id))
        return TemplateResponse("auth/profile.html", context={"user": user})

    @post("/logout")
    async def logout(self, request: Request) -> Redirect:
        request.session.clear()
        return Redirect("/")
```

### Controller with Flash Messages

```python
@post("/items")
async def create_item(
    self, request: Request, db_session: AsyncSession, data: ItemCreate
) -> Redirect:
    await item_service.create(db_session, data)
    request.session["flash"] = {"type": "success", "message": "Item created!"}
    return Redirect("/items")

@get("/items")
async def list_items(self, request: Request, db_session: AsyncSession):
    flash = request.session.pop("flash", None)  # Get and remove
    items = await item_service.list_items(db_session)
    return TemplateResponse("items/list.html", context={"items": items, "flash": flash})
```

## Database Model Patterns

### Basic Model

```python
from sqlalchemy import String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from skrift.db.base import Base

class Article(Base):
    __tablename__ = "articles"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
```

### Model with Relationships

```python
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

class Comment(Base):
    __tablename__ = "comments"

    # Foreign key
    article_id: Mapped[UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationship
    article: Mapped["Article"] = relationship("Article", back_populates="comments")

    content: Mapped[str] = mapped_column(Text, nullable=False)

# In Article model:
class Article(Base):
    # ...
    comments: Mapped[list["Comment"]] = relationship(
        "Comment",
        back_populates="article",
        cascade="all, delete-orphan",
        order_by="desc(Comment.created_at)",
    )
```

### Model with Optional Fields

```python
from datetime import datetime

class Event(Base):
    __tablename__ = "events"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional fields use | None
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

### Model with Indexes

```python
class LogEntry(Base):
    __tablename__ = "log_entries"

    level: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Composite index
    __table_args__ = (
        Index("ix_log_entries_level_created", "level", "created_at"),
    )
```

## Service Layer Patterns

### Basic CRUD Service

```python
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

async def list_items(
    db_session: AsyncSession,
    limit: int | None = None,
    offset: int = 0,
) -> list[Item]:
    query = select(Item).order_by(Item.created_at.desc())
    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)
    result = await db_session.execute(query)
    return list(result.scalars().all())

async def get_by_id(db_session: AsyncSession, item_id: UUID) -> Item | None:
    result = await db_session.execute(select(Item).where(Item.id == item_id))
    return result.scalar_one_or_none()

async def create(db_session: AsyncSession, name: str, **kwargs) -> Item:
    item = Item(name=name, **kwargs)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item

async def update(
    db_session: AsyncSession,
    item_id: UUID,
    **updates,
) -> Item | None:
    item = await get_by_id(db_session, item_id)
    if not item:
        return None

    for key, value in updates.items():
        if value is not None:
            setattr(item, key, value)

    await db_session.commit()
    await db_session.refresh(item)
    return item

async def delete(db_session: AsyncSession, item_id: UUID) -> bool:
    item = await get_by_id(db_session, item_id)
    if not item:
        return False
    await db_session.delete(item)
    await db_session.commit()
    return True
```

### Service with Filtering

```python
from sqlalchemy import select, and_, or_
from datetime import datetime, UTC

async def list_published_articles(
    db_session: AsyncSession,
    category: str | None = None,
    search: str | None = None,
) -> list[Article]:
    query = select(Article).where(Article.is_published == True)

    filters = []
    if category:
        filters.append(Article.category == category)
    if search:
        filters.append(
            or_(
                Article.title.ilike(f"%{search}%"),
                Article.content.ilike(f"%{search}%"),
            )
        )

    if filters:
        query = query.where(and_(*filters))

    query = query.order_by(Article.published_at.desc())
    result = await db_session.execute(query)
    return list(result.scalars().all())
```

### Service with Hooks

```python
from skrift.lib.hooks import hooks

BEFORE_ITEM_SAVE = "before_item_save"
AFTER_ITEM_SAVE = "after_item_save"

async def create(db_session: AsyncSession, name: str) -> Item:
    item = Item(name=name)

    await hooks.do_action(BEFORE_ITEM_SAVE, item, is_new=True)

    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)

    await hooks.do_action(AFTER_ITEM_SAVE, item, is_new=True)

    return item
```

## Hook/Filter Patterns

### Action Hook

```python
from skrift.lib.hooks import action

@action("after_page_save", priority=10)
async def invalidate_cache(page, is_new: bool):
    """Clear cache when page is saved."""
    cache.delete(f"page:{page.slug}")

@action("after_user_register", priority=5)
async def send_welcome_email(user):
    """Send welcome email to new users."""
    await email_service.send_welcome(user.email)
```

### Filter Hook

```python
from skrift.lib.hooks import filter

@filter("page_seo_meta", priority=10)
async def add_default_author(meta: dict, page) -> dict:
    """Add default author to SEO meta."""
    if "author" not in meta:
        meta["author"] = "Site Author"
    return meta

@filter("template_context", priority=20)
async def add_global_vars(context: dict) -> dict:
    """Add global variables to all templates."""
    context["current_year"] = datetime.now().year
    context["version"] = "1.0.0"
    return context
```

### Custom Hook Points

```python
# Define hook constants
MY_BEFORE_SAVE = "my_before_save"
MY_AFTER_SAVE = "my_after_save"
MY_DATA_FILTER = "my_data_filter"

# Trigger in service
async def save_thing(db_session: AsyncSession, data: dict) -> Thing:
    # Apply filters to data
    data = await hooks.apply_filters(MY_DATA_FILTER, data)

    thing = Thing(**data)
    await hooks.do_action(MY_BEFORE_SAVE, thing)

    db_session.add(thing)
    await db_session.commit()

    await hooks.do_action(MY_AFTER_SAVE, thing)
    return thing
```

## Notification Patterns

### Send a Generic Toast Notification

```python
from skrift.lib.notifications import notify_user, notify_session, _ensure_nid

# To a specific user (persists, cross-device)
notify_user(str(user.id), "generic", title="New follower", message="Alice followed you.")

# To the current session only (persists until dismissed)
nid = _ensure_nid(request)
notify_session(nid, "generic", title="Draft saved", message="Your changes were saved.")
```

### Broadcast an Ephemeral Event

```python
from skrift.lib.notifications import notify_broadcast

# Ephemeral — not stored, won't replay on reconnect
notify_broadcast(
    "new_post",
    post_id=str(post.id),
    author=post.author.name,
    title=post.title,
)
```

### Replace-in-Place with Group Key

```python
from skrift.lib.notifications import notify_session, notify_user, _ensure_nid

# Progress updates — each replaces the previous toast
nid = _ensure_nid(request)
notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 1/3")
notify_session(nid, "generic", group="deploy", title="Deploying…", message="Step 2/3")
notify_session(nid, "generic", group="deploy", title="Deployed!", message="Done")

# User-scoped status update — replaces across all their sessions
notify_user(str(user.id), "generic", group="upload-status", title="Uploading…", message="50%")
notify_user(str(user.id), "generic", group="upload-status", title="Upload complete", message="100%")
```

### Dismiss by Group Key (Backend)

```python
from skrift.lib.notifications import dismiss_session_group, dismiss_user_group

# Dismiss the active "deploy" notification without knowing its UUID
dismiss_session_group(nid, "deploy")

# Dismiss from a user's queue (pushes dismissed event to all their sessions)
dismiss_user_group(str(user.id), "upload-status")
```

### Handle Custom Notification Types (Client-Side)

```javascript
// Listen for custom notification types
document.addEventListener('sk:notification', (e) => {
    const data = e.detail;
    if (data.type !== 'new_post') return;

    // Custom rendering logic
    const list = document.querySelector('.post-list');
    if (list) {
        const el = buildPostCard(data);
        list.prepend(el);
    }

    // Prevent default generic toast rendering
    e.preventDefault();
});
```

### Notify on Action (Controller Pattern)

```python
from skrift.lib.notifications import notify_user

@post("/{item_id:uuid}/comment", guards=[auth_guard])
async def comment(self, request: Request, db_session: AsyncSession, item_id: UUID) -> Redirect:
    user = await self._get_user(request, db_session)
    comment = await comment_service.create(db_session, user.id, item_id, form.data.content)

    # Notify item owner (skip if self-comment)
    item = await item_service.get_by_id(db_session, item_id)
    if item and str(item.user_id) != str(user.id):
        notify_user(
            str(item.user_id),
            "generic",
            title=f"{user.name} commented on your post",
            message=form.data.content[:100],
        )

    return Redirect(path=f"/items/{item_id}")
```

## Form Patterns

### Basic Form Model

```python
from skrift.forms import FormModel

class ContactForm(FormModel, form_name="contact"):
    name: str
    email: str
    message: str
```

### Form with Full Field Customization

```python
from pydantic import Field, EmailStr
from skrift.forms import FormModel

class ContactForm(FormModel, form_name="contact"):
    name: str = Field(json_schema_extra={
        "label": "Your Name",
        "attrs": {"placeholder": "Jane Doe"},
    })
    email: EmailStr = Field(json_schema_extra={
        "label": "Email Address",
        "input_type": "email",
        "help_text": "We'll never share your email.",
    })
    subject: str = Field(json_schema_extra={
        "widget": "select",
        "choices": [
            ("general", "General Inquiry"),
            ("support", "Technical Support"),
        ],
    })
    message: str = Field(json_schema_extra={
        "widget": "textarea",
        "attrs": {"rows": "6"},
    })
    subscribe: bool = Field(default=False, json_schema_extra={
        "label": "Subscribe to newsletter",
    })
```

### Controller GET/POST Pattern

```python
from litestar import Controller, get, post, Request
from litestar.response import Template as TemplateResponse, Redirect
from skrift.forms import Form

class ContactController(Controller):
    path = "/contact"

    @get("/")
    async def show(self, request: Request) -> TemplateResponse:
        form = Form(ContactForm, request)
        return TemplateResponse("contact.html", context={"form": form})

    @post("/")
    async def submit(self, request: Request) -> TemplateResponse | Redirect:
        form = Form(ContactForm, request)
        if await form.validate():
            # form.data is a validated ContactForm instance
            await process_contact(form.data)
            return Redirect("/contact?thanks=1")
        return TemplateResponse("contact.html", context={"form": form})
```

### Custom Form Template

```html
{# templates/form-contact.html #}
<form method="{{ form.method }}" class="contact-form">
    {{ form.csrf_field() }}

    {% if form.form_error %}
        <div class="alert">{{ form.form_error }}</div>
    {% endif %}

    <div class="row">
        <div class="col">
            {{ form['name'].label_tag() }}
            {{ form['name'].widget(class_="form-input") }}
        </div>
        <div class="col">
            {{ form['email'].label_tag() }}
            {{ form['email'].widget(class_="form-input") }}
        </div>
    </div>

    {{ form['message'].label_tag() }}
    {{ form['message'].widget(class_="form-input", rows="8") }}

    <button type="submit">{{ submit_label }}</button>
</form>
```

### Form with Hooks

```python
from skrift.lib.hooks import filter

@filter("form_contact_validated")
async def sanitize_contact(data):
    data.message = data.message.strip()
    return data

@filter("form_validated")
async def log_all_forms(data, name):
    print(f"Form '{name}' submitted")
    return data
```

### Decorator-Based Form

```python
from pydantic import BaseModel
from skrift.forms import form

@form("newsletter", action="/subscribe", method="post")
class NewsletterForm(BaseModel):
    email: str
```

## Template Patterns

### Using Template Class

```python
from skrift.lib.template import Template
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

@get("/{slug:str}")
async def view_item(self, db_session: AsyncSession, slug: str) -> TemplateResponse:
    item = await item_service.get_by_slug(db_session, slug)

    # Tries: item-{slug}.html -> item.html
    template = Template("item", slug, context={"item": item})
    return template.render(TEMPLATE_DIR)
```

### Template with SEO Context

```python
from skrift.lib.seo import get_page_seo_meta, get_page_og_meta

@get("/{slug:path}")
async def view_page(self, request: Request, db_session: AsyncSession, slug: str):
    page = await page_service.get_by_slug(db_session, slug)

    site_name = get_cached_site_name()
    base_url = str(request.base_url).rstrip("/")

    seo_meta = await get_page_seo_meta(page, site_name, base_url)
    og_meta = await get_page_og_meta(page, site_name, base_url)

    return TemplateResponse("page.html", context={
        "page": page,
        "seo_meta": seo_meta,
        "og_meta": og_meta,
    })
```

## Middleware Patterns

### Simple Middleware

```python
# myapp/middleware.py
from litestar.middleware import AbstractMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

class LoggingMiddleware(AbstractMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            print(f"Request: {scope['method']} {scope['path']}")
        await self.app(scope, receive, send)

def create_logging_middleware(app: ASGIApp) -> ASGIApp:
    return LoggingMiddleware(app=app)
```

Register in app.yaml:
```yaml
middleware:
  - myapp.middleware:create_logging_middleware
```

### Middleware with Configuration

```python
from litestar.middleware import DefineMiddleware

class RateLimitMiddleware(AbstractMiddleware):
    def __init__(self, app: ASGIApp, requests_per_minute: int = 60):
        super().__init__(app)
        self.limit = requests_per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Rate limiting logic...
        await self.app(scope, receive, send)

def create_rate_limit_middleware(app: ASGIApp, requests_per_minute: int = 60) -> ASGIApp:
    return RateLimitMiddleware(app, requests_per_minute)
```

Register with kwargs:
```yaml
middleware:
  - factory: myapp.middleware:create_rate_limit_middleware
    kwargs:
      requests_per_minute: 100
```

## Authorization Patterns

### Permission-Based Access

```python
from skrift.auth import auth_guard, Permission

class ArticleController(Controller):
    path = "/articles"
    guards = [auth_guard]  # All routes require auth

    @get("/")
    async def list_articles(self, db_session: AsyncSession):
        # Anyone authenticated can list
        ...

    @post("/", guards=[Permission("create-articles")])
    async def create_article(self, db_session: AsyncSession, data: ArticleCreate):
        # Only users with create-articles permission
        ...

    @delete("/{id:uuid}", guards=[Permission("delete-articles")])
    async def delete_article(self, db_session: AsyncSession, id: UUID):
        # Only users with delete-articles permission
        ...
```

### Custom Role Registration

```python
# In your app's __init__.py or startup module
from skrift.auth import register_role

# Register before database sync (app startup)
register_role(
    "contributor",
    "create-articles",
    "edit-own-articles",
    display_name="Contributor",
    description="Can create and edit their own articles",
)

register_role(
    "reviewer",
    "view-drafts",
    "approve-articles",
    display_name="Reviewer",
    description="Can review and approve articles",
)
```

## Testing Patterns

### Controller Test

```python
import pytest
from litestar.testing import TestClient

@pytest.fixture
def client(app):
    return TestClient(app)

async def test_list_items(client, db_session):
    # Create test data
    item = await item_service.create(db_session, name="Test")

    response = client.get("/items")
    assert response.status_code == 200
    assert "Test" in response.text
```

### Service Test

```python
import pytest

async def test_create_item(db_session):
    item = await item_service.create(db_session, name="Test Item")

    assert item.id is not None
    assert item.name == "Test Item"

async def test_list_items_filters(db_session):
    await item_service.create(db_session, name="Item A", published=True)
    await item_service.create(db_session, name="Item B", published=False)

    published = await item_service.list_items(db_session, published_only=True)

    assert len(published) == 1
    assert published[0].name == "Item A"
```

### Hook Test

```python
from skrift.lib.hooks import hooks

async def test_hook_called(db_session):
    called_with = []

    async def track_save(page, is_new):
        called_with.append((page.title, is_new))

    hooks.add_action("after_page_save", track_save)

    try:
        await page_service.create(db_session, slug="test", title="Test")

        assert len(called_with) == 1
        assert called_with[0] == ("Test", True)
    finally:
        hooks.remove_action("after_page_save", track_save)
```
