---
name: skrift-db
description: "Skrift database layer — SQLAlchemy models, services, Alembic migrations, and query patterns."
---

# Skrift Database Layer

SQLAlchemy async ORM with Advanced Alchemy, Alembic migrations, and module-level service functions.

## Current State

**Models:**
!`ls skrift/db/models/*.py 2>/dev/null | head -15`

**Services:**
!`ls skrift/db/services/*.py 2>/dev/null | head -10`

**Recent migrations:**
!`ls skrift/alembic/versions/*.py 2>/dev/null | tail -5`

## Base Model

All models inherit from `skrift.db.base.Base` (provides `id` UUID, `created_at`, `updated_at`):

```python
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from skrift.db.base import Base

class MyModel(Base):
    __tablename__ = "my_models"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
```

## Core Models

| Model | Table | Purpose |
|-------|-------|---------|
| `User` | `users` | User accounts |
| `OAuthAccount` | `oauth_accounts` | Linked OAuth providers (`access_token`, `refresh_token`) |
| `Role` | `roles` | Permission roles |
| `RolePermission` | `role_permissions` | Role-to-permission mapping |
| `Page` | `pages` | Content pages |
| `PageRevision` | `page_revisions` | Content history |
| `PageAsset` | `page_assets` | Page-to-asset relationships |
| `Asset` | `assets` | Media/file assets |
| `Setting` | `settings` | Key-value site settings |
| `StoredNotification` | `stored_notifications` | Persistent notifications (Redis/PgNotify backends) |
| `DismissedNotification` | `dismissed_notifications` | Dismissed notification tracking |
| `OAuth2Client` | `oauth2_clients` | Registered OAuth2 client applications |
| `RevokedToken` | `revoked_tokens` | Revoked token JTIs for OAuth2 server |
| `PushSubscription` | `push_subscriptions` | Web push notification subscriptions |

## Creating a Service

Services are module-level async functions that take `db_session`:

```python
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

async def get_by_id(db_session: AsyncSession, item_id: UUID) -> MyModel | None:
    result = await db_session.execute(select(MyModel).where(MyModel.id == item_id))
    return result.scalar_one_or_none()

async def create_item(db_session: AsyncSession, name: str) -> MyModel:
    item = MyModel(name=name)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item
```

## Session Injection

Handlers receive the database session via Litestar dependency injection:

```python
from sqlalchemy.ext.asyncio import AsyncSession

@get("/items/{item_id:uuid}")
async def get_item(self, db_session: AsyncSession, item_id: UUID) -> TemplateResponse:
    item = await item_service.get_by_id(db_session, item_id)
    ...
```

## Database Configuration

```yaml
db:
  url: $DATABASE_URL
  pool_size: 5
  echo: false
  schema: myschema  # optional; PostgreSQL only — prefixes all tables
```

The `schema` setting applies a PostgreSQL schema prefix to all tables. Omit for SQLite or default public schema.

## Alembic Migrations

Migrations live in `skrift/alembic/`. Commands via the Skrift CLI:

```bash
skrift db upgrade head              # Apply all pending migrations
skrift db downgrade -1              # Roll back one migration
skrift db revision -m "desc" --autogenerate  # Generate from model changes
skrift db revision -m "desc"        # Empty migration for manual SQL
skrift db current                   # Show current revision
skrift db history                   # Show migration history
```

The `env.py` imports all models from `skrift.db.models` and uses the async engine from settings.

## Content Negotiation

Page views support `Accept: text/markdown` — returns raw `page.content` instead of rendered HTML. Works for all page types (WebController and page type factory routes).

## Key Files

| File | Purpose |
|------|---------|
| `skrift/db/base.py` | SQLAlchemy Base class (UUIDAuditBase) |
| `skrift/db/models/` | All ORM models |
| `skrift/db/services/` | Data access layer (module-level functions) |
| `skrift/db/session.py` | Database session management, SessionCleanupMiddleware |
| `skrift/alembic/` | Alembic config and migration versions |
| `skrift/config.py` | `DatabaseConfig` model |
