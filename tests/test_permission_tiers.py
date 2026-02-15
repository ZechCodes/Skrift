"""Tests for permission tiers, OwnerOrPermission guard, and admin helpers."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from skrift.auth.guards import OwnerOrPermission, Permission
from skrift.auth.roles import ADMIN, AUTHOR, EDITOR, MODERATOR


class TestRolePermissions:
    """Verify role definitions include expected permissions."""

    def test_author_has_own_page_permissions(self):
        assert "edit-own-pages" in AUTHOR.permissions
        assert "delete-own-pages" in AUTHOR.permissions
        assert "create-pages" in AUTHOR.permissions

    def test_author_does_not_have_manage_pages(self):
        assert "manage-pages" not in AUTHOR.permissions

    def test_editor_has_manage_pages_and_create(self):
        assert "manage-pages" in EDITOR.permissions
        assert "create-pages" in EDITOR.permissions

    def test_moderator_has_manage_pages_and_create(self):
        assert "manage-pages" in MODERATOR.permissions
        assert "create-pages" in MODERATOR.permissions

    def test_admin_has_administrator(self):
        assert "administrator" in ADMIN.permissions


class TestOwnerOrPermission:
    @pytest.fixture
    def guard(self):
        return OwnerOrPermission("edit-own-pages", "manage-pages")

    @pytest.mark.asyncio
    async def test_admin_always_passes(self, guard):
        perms = MagicMock()
        perms.permissions = {"administrator"}
        assert await guard.check(perms) is True

    @pytest.mark.asyncio
    async def test_any_permission_passes(self, guard):
        perms = MagicMock()
        perms.permissions = {"manage-pages"}
        assert await guard.check(perms) is True

    @pytest.mark.asyncio
    async def test_own_permission_passes(self, guard):
        perms = MagicMock()
        perms.permissions = {"edit-own-pages"}
        assert await guard.check(perms) is True

    @pytest.mark.asyncio
    async def test_no_permission_fails(self, guard):
        perms = MagicMock()
        perms.permissions = {"view-drafts"}
        assert await guard.check(perms) is False


class TestExtractPageFormData:
    def test_valid_data(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": " My Page ",
            "slug": " my-page ",
            "content": "Content here",
            "is_published": "on",
            "order": "5",
            "publish_at": "2026-01-15T10:00:00",
            "meta_description": "Description",
            "og_title": "",
            "og_description": "",
            "og_image": "",
            "meta_robots": "",
        }
        result = extract_page_form_data(data)
        assert result.title == "My Page"
        assert result.slug == "my-page"
        assert result.is_published is True
        assert result.order == 5
        assert result.publish_at is not None
        assert result.meta_description == "Description"
        assert result.og_title is None  # empty string becomes None

    def test_missing_title_returns_empty(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {"title": "", "slug": "slug", "content": "c"}
        result = extract_page_form_data(data)
        assert result.title == ""

    def test_invalid_datetime_raises(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {
            "title": "Page",
            "slug": "page",
            "content": "",
            "publish_at": "not-a-date",
        }
        with pytest.raises(ValueError, match="Invalid publish date"):
            extract_page_form_data(data)

    def test_default_order_zero(self):
        from skrift.admin.helpers import extract_page_form_data

        data = {"title": "Page", "slug": "page", "content": ""}
        result = extract_page_form_data(data)
        assert result.order == 0


class TestCheckPageAccess:
    @pytest.mark.asyncio
    async def test_admin_can_access_any_page(self):
        from skrift.admin.helpers import check_page_access

        mock_session = AsyncMock()
        mock_request = MagicMock()
        mock_request.session = {"user_id": str(uuid4())}
        mock_page = MagicMock()

        with patch("skrift.admin.helpers.get_user_permissions") as mock_get_perms:
            mock_perms = MagicMock()
            mock_perms.permissions = {"administrator"}
            mock_get_perms.return_value = mock_perms

            # Should not raise
            await check_page_access(
                mock_session, mock_request, mock_page,
                "edit-own-pages", "manage-pages"
            )

    @pytest.mark.asyncio
    async def test_editor_can_access_any_page(self):
        from skrift.admin.helpers import check_page_access

        mock_session = AsyncMock()
        mock_request = MagicMock()
        mock_request.session = {"user_id": str(uuid4())}
        mock_page = MagicMock()

        with patch("skrift.admin.helpers.get_user_permissions") as mock_get_perms:
            mock_perms = MagicMock()
            mock_perms.permissions = {"manage-pages"}
            mock_get_perms.return_value = mock_perms

            await check_page_access(
                mock_session, mock_request, mock_page,
                "edit-own-pages", "manage-pages"
            )

    @pytest.mark.asyncio
    async def test_author_can_access_own_page(self):
        from skrift.admin.helpers import check_page_access

        user_id = uuid4()
        mock_session = AsyncMock()
        mock_request = MagicMock()
        mock_request.session = {"user_id": str(user_id)}
        mock_page = MagicMock()
        mock_page.id = uuid4()

        with patch("skrift.admin.helpers.get_user_permissions") as mock_get_perms, \
             patch("skrift.admin.helpers.page_service") as mock_ps:
            mock_perms = MagicMock()
            mock_perms.permissions = {"edit-own-pages"}
            mock_get_perms.return_value = mock_perms
            mock_ps.check_page_ownership = AsyncMock(return_value=True)

            await check_page_access(
                mock_session, mock_request, mock_page,
                "edit-own-pages", "manage-pages"
            )

    @pytest.mark.asyncio
    async def test_author_denied_for_others_page(self):
        from litestar.exceptions import NotAuthorizedException
        from skrift.admin.helpers import check_page_access

        user_id = uuid4()
        mock_session = AsyncMock()
        mock_request = MagicMock()
        mock_request.session = {"user_id": str(user_id)}
        mock_page = MagicMock()
        mock_page.id = uuid4()

        with patch("skrift.admin.helpers.get_user_permissions") as mock_get_perms, \
             patch("skrift.admin.helpers.page_service") as mock_ps:
            mock_perms = MagicMock()
            mock_perms.permissions = {"edit-own-pages"}
            mock_get_perms.return_value = mock_perms
            mock_ps.check_page_ownership = AsyncMock(return_value=False)

            with pytest.raises(NotAuthorizedException):
                await check_page_access(
                    mock_session, mock_request, mock_page,
                    "edit-own-pages", "manage-pages"
                )
