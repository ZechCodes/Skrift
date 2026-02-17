"""Tests for admin controllers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


class TestGetAdminContext:
    @pytest.mark.asyncio
    async def test_raises_without_user_id(self):
        """Should raise NotAuthorizedException if no user_id in session."""
        from litestar.exceptions import NotAuthorizedException
        from skrift.admin.helpers import get_admin_context

        request = MagicMock()
        request.session = {}
        db_session = AsyncMock()

        with pytest.raises(NotAuthorizedException):
            await get_admin_context(request, db_session)

    @pytest.mark.asyncio
    async def test_raises_for_invalid_user(self):
        """Should raise NotAuthorizedException if user not found in DB."""
        from litestar.exceptions import NotAuthorizedException
        from skrift.admin.helpers import get_admin_context

        user_id = str(uuid4())
        request = MagicMock()
        request.session = {"user_id": user_id}
        request.url.path = "/admin"

        db_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db_session.execute.return_value = mock_result

        with patch("skrift.admin.helpers.select"), \
             pytest.raises(NotAuthorizedException):
            await get_admin_context(request, db_session)

    @pytest.mark.asyncio
    async def test_returns_context_with_nav(self):
        """Should return context dict with user, permissions, and nav."""
        from skrift.admin.helpers import get_admin_context

        user_id = str(uuid4())
        mock_user = MagicMock()
        mock_user.id = user_id

        request = MagicMock()
        request.session = {"user_id": user_id}
        request.url.path = "/admin"

        db_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        db_session.execute.return_value = mock_result

        mock_perms = MagicMock()
        mock_nav = [MagicMock()]

        with patch("skrift.admin.helpers.select"), \
             patch("skrift.admin.helpers.get_user_permissions", new_callable=AsyncMock, return_value=mock_perms), \
             patch("skrift.admin.helpers.build_admin_nav", new_callable=AsyncMock, return_value=mock_nav):
            ctx = await get_admin_context(request, db_session)

        assert ctx["user"] is mock_user
        assert ctx["permissions"] is mock_perms
        assert ctx["admin_nav"] == mock_nav
        assert ctx["current_path"] == "/admin"


class TestExtractPageFormData:
    def test_complete_valid_data(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "My Page",
            "slug": "my-page",
            "content": "Content",
            "is_published": "on",
            "order": "3",
            "publish_at": "2026-06-15T12:00:00",
            "meta_description": "SEO desc",
            "og_title": "OG Title",
            "og_description": "OG Desc",
            "og_image": "https://img.url",
            "meta_robots": "noindex",
        }
        result = extract_page_form_data(data)
        assert result.title == "My Page"
        assert result.is_published is True
        assert result.order == 3
        assert result.publish_at is not None
        assert result.meta_description == "SEO desc"
        assert result.meta_robots == "noindex"

    def test_empty_optional_fields_become_none(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "Page",
            "slug": "page",
            "content": "",
            "og_title": "",
            "og_description": "  ",
            "og_image": "",
            "meta_robots": "",
            "meta_description": "",
        }
        result = extract_page_form_data(data)
        assert result.og_title is None
        assert result.og_description is None
        assert result.og_image is None
        assert result.meta_robots is None

    def test_invalid_datetime_raises_valueerror(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "Page",
            "slug": "page",
            "content": "",
            "publish_at": "invalid-date",
        }
        with pytest.raises(ValueError, match="Invalid publish date"):
            extract_page_form_data(data)


class TestRequirePage:
    @pytest.mark.asyncio
    async def test_returns_page_if_found(self):
        from skrift.admin.helpers import require_page

        mock_page = MagicMock()
        page_id = uuid4()

        with patch("skrift.admin.helpers.page_service") as mock_ps:
            mock_ps.get_page_by_id = AsyncMock(return_value=mock_page)
            result = await require_page(AsyncMock(), page_id)
            assert result is mock_page

    @pytest.mark.asyncio
    async def test_raises_if_not_found(self):
        from skrift.admin.helpers import require_page

        with patch("skrift.admin.helpers.page_service") as mock_ps:
            mock_ps.get_page_by_id = AsyncMock(return_value=None)
            with pytest.raises(ValueError, match="Page not found"):
                await require_page(AsyncMock(), uuid4())


class TestPageListFiltering:
    @pytest.mark.asyncio
    async def test_editor_sees_all_pages(self):
        """Editor with manage-pages should see all pages."""
        from skrift.admin.page_type_factory import create_page_type_controller
        from skrift.config import PageTypeConfig

        PageController = create_page_type_controller(
            PageTypeConfig(name="page", plural="pages")
        )

        user_id = str(uuid4())
        mock_perms = MagicMock()
        mock_perms.permissions = {"manage-pages"}

        controller = PageController(owner=MagicMock())
        request = MagicMock()
        request.session = {"user_id": user_id}
        request.url.path = "/admin/pages"

        db_session = AsyncMock()
        mock_user = MagicMock()

        mock_context = {
            "user": mock_user,
            "permissions": mock_perms,
            "admin_nav": [],
            "current_path": "/admin/pages",
        }

        mock_pages_result = MagicMock()
        mock_pages_result.scalars.return_value.all.return_value = [MagicMock(), MagicMock()]

        with patch("skrift.admin.page_type_factory.get_admin_context", new_callable=AsyncMock, return_value=mock_context), \
             patch("skrift.admin.page_type_factory.get_flash_messages", return_value=[]), \
             patch("skrift.admin.page_type_factory.select") as mock_select:
            mock_query = MagicMock()
            mock_select.return_value = mock_query
            mock_query.where.return_value = mock_query
            mock_query.options.return_value = mock_query
            mock_query.order_by.return_value = mock_query
            db_session.execute.return_value = mock_pages_result

            result = await PageController.list_pages.fn(
                controller, request, db_session
            )
            assert result.template_name == "admin/pages/list.html"
