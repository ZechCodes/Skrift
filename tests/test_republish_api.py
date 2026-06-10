"""Integration tests for the republish HTTP API and admin locking.

Exercises PUT/DELETE /api/republish/posts end-to-end against an in-memory
database: grant-constrained API key auth, upsert/update semantics, delete
behaviors, and the PAGE_ADMIN_CAN_MUTATE / PAGE_ADMIN_PAGE_STATE hooks that
lock republished pages in the admin.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from litestar import Litestar
from litestar.testing import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from skrift.db.base import Base
from skrift.db.models import Page, PageRepublish, User
from skrift.db.services import api_key_service
from skrift.republish import REPUBLISH_PERMISSION
from skrift.republish.controller import RepublishController
from skrift.republish.hooks import (
    mark_republish_admin_state,
    prevent_republish_admin_mutation,
)

SOURCE_ORIGIN = "https://source.example"
CANONICAL_URL = f"{SOURCE_ORIGIN}/posts/hello"


def _settings(*, delete_behavior: str = "unpublish"):
    return SimpleNamespace(
        republish=SimpleNamespace(
            enabled=True,
            discovery_enabled=True,
            default_page_type="post",
            page_types=["post"],
            default_post_behavior="publish",
            default_delete_behavior=delete_behavior,
        )
    )


def _constraints(*, post_behavior: str = "publish", delete_behavior: str = "unpublish"):
    return {
        "republish": {
            "schema": "baseline-v1",
            "source_origin": SOURCE_ORIGIN,
            "page_type": "post",
            "post_behavior": post_behavior,
            "delete_behavior": delete_behavior,
        }
    }


@pytest.fixture
def engine():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
def app(engine):
    db_config = SQLAlchemyAsyncConfig(
        engine_instance=engine,
        metadata=Base.metadata,
        create_all=True,
        session_config=AsyncSessionConfig(expire_on_commit=False),
    )
    return Litestar(
        route_handlers=[RepublishController],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        debug=True,
    )


@pytest.fixture
def client(app):
    with TestClient(app=app) as c:
        yield c


def _run(client, coro):
    """Run a coroutine on the app's event loop (shares aiosqlite connection)."""

    async def _inner():
        return await coro

    return client.blocking_portal.call(_inner)


async def _seed_user_and_key(engine, *, constraints) -> str:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        user = User(
            email=f"author-{uuid4().hex[:8]}@example.com",
            name="Author",
        )
        db.add(user)
        await db.commit()
        _, raw_key, _ = await api_key_service.create_api_key(
            db,
            user.id,
            "Republish grant key",
            scoped_permissions=[REPUBLISH_PERMISSION],
            grant_source="api-grant",
            constraints=constraints,
        )
        return raw_key


async def _fetch_one(engine, stmt):
    async with AsyncSession(engine, expire_on_commit=False) as db:
        result = await db.execute(stmt)
        return result.scalar_one_or_none()


def _payload(**overrides):
    payload = {
        "canonical_url": CANONICAL_URL,
        "title": "Hello",
        "content": "<p>Hi</p>",
        "summary": "A post",
        "author_name": "Author",
        "tags": ["demo"],
        "updated_at": "2026-06-01T12:00:00Z",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def republish_settings():
    settings = _settings()
    with (
        patch("skrift.republish.controller.get_settings", return_value=settings),
        patch("skrift.republish.hooks.get_settings", return_value=settings),
    ):
        yield settings


class TestUpsertPost:
    def test_put_creates_then_updates(self, client, engine, republish_settings):
        raw_key = _run(client, _seed_user_and_key(engine, constraints=_constraints()))
        headers = {"Authorization": f"Bearer {raw_key}"}

        created = client.put("/api/republish/posts", json=_payload(), headers=headers)
        assert created.status_code == 201
        assert created.json()["status"] == "created"
        assert created.json()["canonical_url"] == CANONICAL_URL

        updated = client.put(
            "/api/republish/posts",
            json=_payload(title="Hello again"),
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "updated"

        page = _run(
            client,
            _fetch_one(engine, select(Page).where(Page.type == "post")),
        )
        assert page.title == "Hello again"
        assert page.is_published is True

        republish = _run(
            client,
            _fetch_one(
                engine,
                select(PageRepublish).where(
                    PageRepublish.canonical_url == CANONICAL_URL
                ),
            ),
        )
        assert republish.source_origin == SOURCE_ORIGIN
        assert republish.page_id == page.id

    def test_draft_post_behavior_creates_unpublished_page(
        self, client, engine, republish_settings
    ):
        raw_key = _run(
            client,
            _seed_user_and_key(
                engine, constraints=_constraints(post_behavior="draft")
            ),
        )
        response = client.put(
            "/api/republish/posts",
            json=_payload(),
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert response.status_code == 201

        page = _run(
            client, _fetch_one(engine, select(Page).where(Page.type == "post"))
        )
        assert page.is_published is False

    def test_canonical_url_origin_must_match_key(
        self, client, engine, republish_settings
    ):
        raw_key = _run(client, _seed_user_and_key(engine, constraints=_constraints()))
        response = client.put(
            "/api/republish/posts",
            json=_payload(canonical_url="https://other.example/posts/hello"),
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "invalid_request"

    def test_missing_bearer_rejected(self, client, republish_settings):
        response = client.put("/api/republish/posts", json=_payload())
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_client"

    def test_key_without_republish_constraints_rejected(
        self, client, engine, republish_settings
    ):
        raw_key = _run(client, _seed_user_and_key(engine, constraints=None))
        response = client.put(
            "/api/republish/posts",
            json=_payload(),
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "invalid_scope"


class TestDeletePost:
    def _create(self, client, engine, *, delete_behavior: str) -> str:
        raw_key = _run(
            client,
            _seed_user_and_key(
                engine, constraints=_constraints(delete_behavior=delete_behavior)
            ),
        )
        headers = {"Authorization": f"Bearer {raw_key}"}
        assert (
            client.put(
                "/api/republish/posts", json=_payload(), headers=headers
            ).status_code
            == 201
        )
        return raw_key

    def test_delete_unpublish_behavior(self, client, engine, republish_settings):
        raw_key = self._create(client, engine, delete_behavior="unpublish")
        response = client.request(
            "DELETE",
            "/api/republish/posts",
            json={"canonical_url": CANONICAL_URL},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "unpublished"

        page = _run(
            client, _fetch_one(engine, select(Page).where(Page.type == "post"))
        )
        assert page.is_published is False

    def test_delete_delete_behavior_removes_page(
        self, client, engine, republish_settings
    ):
        raw_key = self._create(client, engine, delete_behavior="delete")
        response = client.request(
            "DELETE",
            "/api/republish/posts",
            json={"canonical_url": CANONICAL_URL},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"

        page = _run(
            client, _fetch_one(engine, select(Page).where(Page.type == "post"))
        )
        assert page is None

    def test_delete_unknown_canonical_url_404(
        self, client, engine, republish_settings
    ):
        raw_key = _run(client, _seed_user_and_key(engine, constraints=_constraints()))
        response = client.request(
            "DELETE",
            "/api/republish/posts",
            json={"canonical_url": f"{SOURCE_ORIGIN}/posts/nope"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert response.status_code == 404


class TestAdminLocking:
    def test_republished_page_is_locked_and_badged(
        self, client, engine, republish_settings
    ):
        raw_key = _run(client, _seed_user_and_key(engine, constraints=_constraints()))
        client.put(
            "/api/republish/posts",
            json=_payload(),
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        page = _run(
            client, _fetch_one(engine, select(Page).where(Page.type == "post"))
        )

        async def _check():
            async with AsyncSession(engine, expire_on_commit=False) as db:
                allowed = await prevent_republish_admin_mutation(
                    True, MagicMock(), db, page, "edit"
                )
                state = await mark_republish_admin_state(
                    {"locked": False, "badges": []}, MagicMock(), db, page
                )
                return allowed, state

        allowed, state = _run(client, _check())
        assert allowed is False
        assert state["locked"] is True
        assert "Repost" in state["badges"]

    def test_ordinary_page_is_not_locked(self, client, engine, republish_settings):
        async def _check():
            async with AsyncSession(engine, expire_on_commit=False) as db:
                user = User(email="plain@example.com", name="Plain")
                db.add(user)
                await db.commit()
                page = Page(
                    slug="plain",
                    title="Plain",
                    content="x",
                    type="post",
                    user_id=user.id,
                )
                db.add(page)
                await db.commit()
                allowed = await prevent_republish_admin_mutation(
                    True, MagicMock(), db, page, "edit"
                )
                state = await mark_republish_admin_state(
                    {"locked": False, "badges": []}, MagicMock(), db, page
                )
                return allowed, state

        allowed, state = _run(client, _check())
        assert allowed is True
        assert state["locked"] is False
