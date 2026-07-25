"""The sitemap must not materialize page bodies or page relationships.

``/sitemap.xml`` is publicly reachable and hit by every crawler, so the query
behind it is projected down to the three columns the XML actually uses. These
tests pin that projection: no ``pages.content``, no ``selectin`` relationship
fan-out, and a hard row cap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.controllers.sitemap import SitemapController
from skrift.db.base import Base
from skrift.db.models import Page  # noqa: F401  (registers table)
from skrift.db.services import page_service


@pytest_asyncio.fixture
async def sitemap_db():
    """A SQLite session that records every statement it executes."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    executed_statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record_statement(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield SimpleNamespace(session=session, statements=executed_statements)
    await engine.dispose()


async def seed_page(session, slug: str, **overrides) -> Page:
    """Insert a published page with a large body unless overridden."""
    values = {
        "slug": slug,
        "title": slug.title(),
        "content": "x" * 10_000,
        "is_published": True,
        "order": 0,
    }
    values.update(overrides)
    page = Page(**values)
    session.add(page)
    await session.commit()
    return page


class TestListPageSitemapEntries:
    @pytest.mark.asyncio
    async def test_returns_only_published_and_already_scheduled_pages(self, sitemap_db):
        await seed_page(sitemap_db.session, "public")
        await seed_page(sitemap_db.session, "draft", is_published=False)
        await seed_page(
            sitemap_db.session,
            "scheduled",
            publish_at=datetime.now(UTC) + timedelta(days=1),
        )

        entries = await page_service.list_page_sitemap_entries(sitemap_db.session)

        assert [entry.slug for entry in entries] == ["public"]

    @pytest.mark.asyncio
    async def test_entries_expose_only_the_sitemap_columns(self, sitemap_db):
        await seed_page(sitemap_db.session, "public")

        entry = (await page_service.list_page_sitemap_entries(sitemap_db.session))[0]

        assert entry.slug == "public"
        assert entry.created_at is not None
        assert not hasattr(entry, "content")
        assert not hasattr(entry, "assets")

    @pytest.mark.asyncio
    async def test_query_selects_no_body_and_triggers_no_relationship_loads(
        self, sitemap_db
    ):
        await seed_page(sitemap_db.session, "public")
        sitemap_db.statements.clear()

        await page_service.list_page_sitemap_entries(sitemap_db.session)

        assert len(sitemap_db.statements) == 1
        statement = sitemap_db.statements[0].lower()
        assert "pages.content" not in statement
        assert "assets" not in statement
        assert "page_republish" not in statement

    @pytest.mark.asyncio
    async def test_row_count_is_capped(self, sitemap_db):
        for index in range(5):
            await seed_page(sitemap_db.session, f"page-{index}")

        entries = await page_service.list_page_sitemap_entries(
            sitemap_db.session, limit=2
        )

        assert len(entries) == 2

    def test_default_limit_is_the_sitemap_protocol_maximum(self):
        limit_parameter = signature(
            page_service.list_page_sitemap_entries
        ).parameters["limit"]

        assert page_service.SITEMAP_MAX_URLS == 50_000
        assert limit_parameter.default == page_service.SITEMAP_MAX_URLS

    @pytest.mark.asyncio
    async def test_non_positive_limit_is_rejected(self, sitemap_db):
        with pytest.raises(ValueError):
            await page_service.list_page_sitemap_entries(sitemap_db.session, limit=0)


class TestSitemapControllerOutput:
    @pytest.mark.asyncio
    async def test_xml_lists_published_pages_with_last_modified(
        self, sitemap_db, clean_hooks
    ):
        await seed_page(sitemap_db.session, "about", order=1)
        await seed_page(sitemap_db.session, "", order=0)
        await seed_page(sitemap_db.session, "draft", is_published=False)

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()
        request.base_url = "https://example.com/"

        with patch(
            "skrift.controllers.sitemap.get_cached_site_base_url",
            return_value="https://example.com",
        ):
            response = await controller.sitemap.fn(
                controller, request, sitemap_db.session
            )

        xml = response.content
        assert b"<loc>https://example.com</loc>" in xml
        assert b"<loc>https://example.com/about</loc>" in xml
        assert b"draft" not in xml
        assert b"<priority>1.0</priority>" in xml
        assert b"<priority>0.8</priority>" in xml
        assert b"<lastmod>" in xml

    @pytest.mark.asyncio
    async def test_sitemap_page_filter_still_receives_each_page(
        self, sitemap_db, clean_hooks
    ):
        from skrift.hooks import hooks

        await seed_page(sitemap_db.session, "keep")
        await seed_page(sitemap_db.session, "drop")

        def drop_by_slug(entry, page):
            return None if page.slug == "drop" else entry

        hooks.add_filter("sitemap_page", drop_by_slug)

        controller = SitemapController(owner=MagicMock())
        request = MagicMock()
        request.base_url = "https://example.com/"

        with patch(
            "skrift.controllers.sitemap.get_cached_site_base_url",
            return_value="https://example.com",
        ):
            response = await controller.sitemap.fn(
                controller, request, sitemap_db.session
            )

        assert b"/keep" in response.content
        assert b"/drop" not in response.content
