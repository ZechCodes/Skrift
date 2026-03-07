"""Tests for extracted page orchestration helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skrift.admin.helpers import PageFormData


def make_form(**overrides) -> PageFormData:
    """Create a default valid page form payload for tests."""
    values = {
        "title": "Title",
        "slug": "title",
        "content": "Content",
        "is_published": False,
        "order": 1,
        "publish_at": None,
        "meta_description": None,
        "og_title": None,
        "og_description": None,
        "og_image": None,
        "meta_robots": None,
        "asset_ids": [],
        "featured_asset_id": None,
    }
    values.update(overrides)
    return PageFormData(**values)


class TestListPagesForAdmin:
    @pytest.mark.asyncio
    async def test_non_managers_are_scoped_to_own_pages(self):
        from skrift.admin.page_operations import list_pages_for_admin

        db_session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db_session.execute.return_value = result

        user_id = uuid4()
        await list_pages_for_admin(
            db_session,
            page_type_name="post",
            user_id=user_id,
            permissions=SimpleNamespace(permissions={"edit-own-posts"}),
            manage_permission="manage-posts",
        )

        query = db_session.execute.await_args.args[0]
        assert user_id in query.compile().params.values()


class TestTypedPageMutations:
    @pytest.mark.asyncio
    async def test_create_typed_page_syncs_assets_and_featured_asset(self):
        from skrift.admin.page_operations import create_typed_page

        page_id = uuid4()
        featured_asset_id = uuid4()
        attached_asset_id = uuid4()
        form = make_form(
            is_published=True,
            asset_ids=[str(attached_asset_id)],
            featured_asset_id=str(featured_asset_id),
        )

        with (
            patch("skrift.admin.page_operations.page_service.create_page", new_callable=AsyncMock) as mock_create,
            patch("skrift.admin.page_operations.sync_page_assets", new_callable=AsyncMock) as mock_sync,
        ):
            mock_create.return_value = SimpleNamespace(id=page_id)

            await create_typed_page(
                AsyncMock(),
                form=form,
                user_id=uuid4(),
                page_type_name="post",
            )

        assert mock_create.await_args.kwargs["featured_asset_id"] == featured_asset_id
        assert mock_create.await_args.kwargs["published_at"] is not None
        mock_sync.assert_awaited_once_with(ANY, page_id, [attached_asset_id])

    @pytest.mark.asyncio
    async def test_update_typed_page_passes_user_id_for_revisions(self):
        from skrift.admin.page_operations import update_typed_page

        page_id = uuid4()
        acting_user_id = uuid4()
        page = SimpleNamespace(
            id=page_id,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            is_published=False,
        )
        form = make_form(is_published=True, asset_ids=[])

        with (
            patch("skrift.admin.page_operations.page_service.update_page", new_callable=AsyncMock) as mock_update,
            patch("skrift.admin.page_operations.sync_page_assets", new_callable=AsyncMock) as mock_sync,
        ):
            await update_typed_page(
                AsyncMock(),
                page=page,
                form=form,
                user_id=acting_user_id,
                page_type_name="post",
            )

        assert mock_update.await_args.kwargs["user_id"] == acting_user_id
        assert mock_update.await_args.kwargs["page_id"] == page_id
        mock_sync.assert_awaited_once()
