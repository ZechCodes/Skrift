"""ASGI-level tests for request-scoped primary-key DB caching."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from advanced_alchemy.extensions.litestar import (
    AsyncSessionConfig,
    SQLAlchemyAsyncConfig,
    SQLAlchemyPlugin,
)
from litestar import Litestar, get
from litestar.testing import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import skrift.db.models  # noqa: F401 - registers all models on Base.metadata
from skrift.db.base import Base
from skrift.db.cache import get_by_pk
from skrift.db.models import Page


def _run(coro):
    """Run async setup code for sync TestClient tests."""
    return asyncio.run(coro)


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _dispose_engine(engine) -> None:
    await engine.dispose()


async def _insert_page(engine) -> UUID:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        page = Page(slug=f"cached-{uuid4()}", title="Cached", content="")
        session.add(page)
        await session.commit()
        return page.id


def _is_page_pk_select(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return (
        normalized.startswith("select ")
        and " from pages " in normalized
        and " where pages.id = " in normalized
    )


def _build_app():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    page_pk_select_count = 0

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count_page_pk_selects(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        nonlocal page_pk_select_count
        if _is_page_pk_select(statement):
            page_pk_select_count += 1

    @get("/pages/{page_id:str}/twice")
    async def get_page_twice(page_id: str, db_session: AsyncSession) -> dict:
        first = await get_by_pk(db_session, Page, UUID(page_id))
        second = await get_by_pk(db_session, Page, UUID(page_id))
        return {
            "found": first is not None,
            "same_object": first is second,
            "page_pk_select_count": page_pk_select_count,
        }

    db_config = SQLAlchemyAsyncConfig(
        engine_instance=engine,
        metadata=Base.metadata,
        create_all=False,
        session_config=AsyncSessionConfig(expire_on_commit=False),
    )
    app = Litestar(
        route_handlers=[get_page_twice],
        plugins=[SQLAlchemyPlugin(config=db_config)],
        debug=True,
    )
    app.state.engine = engine
    return app


def test_repeated_pk_reads_are_cached_within_one_asgi_request():
    app = _build_app()
    try:
        _run(_create_schema(app.state.engine))
        page_id = _run(_insert_page(app.state.engine))

        with TestClient(app=app) as client:
            response = client.get(f"/pages/{page_id}/twice")
    finally:
        _run(_dispose_engine(app.state.engine))

    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "same_object": True,
        "page_pk_select_count": 1,
    }


def test_pk_cache_is_scoped_to_each_asgi_request():
    app = _build_app()
    try:
        _run(_create_schema(app.state.engine))
        page_id = _run(_insert_page(app.state.engine))

        with TestClient(app=app) as client:
            first = client.get(f"/pages/{page_id}/twice")
            second = client.get(f"/pages/{page_id}/twice")
    finally:
        _run(_dispose_engine(app.state.engine))

    assert first.status_code == 200
    assert first.json()["page_pk_select_count"] == 1
    assert second.status_code == 200
    assert second.json()["page_pk_select_count"] == 2


def test_missing_pk_reads_are_cached_within_one_asgi_request():
    app = _build_app()
    try:
        _run(_create_schema(app.state.engine))
        missing_id = uuid4()

        with TestClient(app=app) as client:
            response = client.get(f"/pages/{missing_id}/twice")
    finally:
        _run(_dispose_engine(app.state.engine))

    assert response.status_code == 200
    assert response.json() == {
        "found": False,
        "same_object": True,
        "page_pk_select_count": 1,
    }
