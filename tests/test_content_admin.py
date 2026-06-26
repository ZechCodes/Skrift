"""Tests for the content admin controller: registration and the save path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from litestar.response import Redirect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from skrift.admin.content import ContentAdminController
from skrift.db.base import Base
from skrift.db.models import ContentAreaRecord  # noqa: F401  (registers table)
from skrift.db.services import content_service


def _raw_fn(handler):
    """Return the underlying async function behind a Litestar route handler."""
    return getattr(handler, "fn", handler)


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


class TestRegistration:
    def test_admin_config_expands_to_include_content_controller(self, mock_config_path):
        from skrift.asgi import load_controllers

        _patched, patcher = mock_config_path(
            {"controllers": ["skrift.admin.controller:AdminController"]}
        )
        try:
            controllers = load_controllers()
        finally:
            patcher.stop()

        assert ContentAdminController in controllers

    def test_content_routes_are_registered(self):
        # The handler decorators register the list, edit, and save routes.
        all_paths = set()
        for handler in (
            ContentAdminController.list_content,
            ContentAdminController.edit_content,
            ContentAdminController.save_content,
        ):
            all_paths.update(handler.paths)
        assert "/content" in all_paths
        assert "/content/{key:str}/edit" in all_paths


class TestSaveHandler:
    @pytest.mark.asyncio
    async def test_save_persists_nested_form_and_redirects(self, db_session):
        request = SimpleNamespace(session={})
        form = {
            "_csrf": "token",
            "hero_title": "Launch day",
            "hero_subtitle": "Async to the core.",
            "cta.label": "Sign up",
            "cta.url": "/signup",
            "sections.0.heading": "Fast",
            "sections.0.body": "Very",
            "sections.1.heading": "Open",
            "sections.1.body": "Yes",
        }

        result = await _raw_fn(ContentAdminController.save_content)(
            ContentAdminController(owner=None),
            request=request,
            db_session=db_session,
            key="home",
            data=form,
        )

        assert isinstance(result, Redirect)
        assert result.url == "/admin/content"

        stored = await content_service.get_content_data(db_session, "home")
        assert stored["hero_title"] == "Launch day"
        assert stored["cta"] == {"label": "Sign up", "url": "/signup"}
        assert [s["heading"] for s in stored["sections"]] == ["Fast", "Open"]

    @pytest.mark.asyncio
    async def test_save_unknown_key_redirects_without_persisting(self, db_session):
        request = SimpleNamespace(session={})

        result = await _raw_fn(ContentAdminController.save_content)(
            ContentAdminController(owner=None),
            request=request,
            db_session=db_session,
            key="not-a-real-area",
            data={"x": "1"},
        )

        assert isinstance(result, Redirect)
        assert result.url == "/admin/content"
        assert request.session.get("flash_messages")
