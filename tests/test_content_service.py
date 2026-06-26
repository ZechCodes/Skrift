"""Integration tests for content persistence against a real SQLite database."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.content import hydrate
from skrift.content.builtin import HomeContent
from skrift.db.base import Base
from skrift.db.models import ContentAreaRecord  # noqa: F401  (registers table)
from skrift.db.services import content_service


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class TestContentService:
    @pytest.mark.asyncio
    async def test_missing_key_returns_empty_dict(self, db_session):
        assert await content_service.get_content_data(db_session, "home") == {}

    @pytest.mark.asyncio
    async def test_save_then_get_roundtrip(self, db_session):
        await content_service.save_content_data(
            db_session, "home", {"hero_title": "Hello"}
        )
        stored = await content_service.get_content_data(db_session, "home")
        assert stored == {"hero_title": "Hello"}

    @pytest.mark.asyncio
    async def test_save_updates_existing_record(self, db_session):
        await content_service.save_content_data(db_session, "home", {"hero_title": "One"})
        await content_service.save_content_data(db_session, "home", {"hero_title": "Two"})
        stored = await content_service.get_content_data(db_session, "home")
        assert stored == {"hero_title": "Two"}

    @pytest.mark.asyncio
    async def test_full_home_content_roundtrips_through_schema(self, db_session):
        model = HomeContent(
            hero_title="Launch day",
            cta={"label": "Sign up", "url": "/signup"},
            sections=[{"heading": "Fast", "body": "Async to the core."}],
        )
        await content_service.save_content_data(
            db_session, "home", model.model_dump(mode="json")
        )

        stored = await content_service.get_content_data(db_session, "home")
        restored = hydrate(HomeContent, stored)
        assert restored.hero_title == "Launch day"
        assert restored.cta.label == "Sign up"
        assert restored.sections[0].heading == "Fast"
