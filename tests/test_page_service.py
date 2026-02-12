"""Tests for the page service module."""

import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4

from skrift.db.services.page_service import (
    published_filter,
    _apply_field_updates,
    _UNSET,
    list_pages,
    get_page_by_slug,
    get_page_by_id,
    create_page,
    update_page,
    delete_page,
    check_page_ownership,
)


@pytest.fixture
def mock_db_session():
    """Create a mock async database session.

    The session's execute method returns a mock result that supports both
    scalar_one_or_none() and scalars().all() access patterns.
    """
    session = AsyncMock()

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value = mock_scalars

    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()

    return session


@pytest.fixture
def mock_page():
    """Create a mock Page object with standard attributes."""
    page = MagicMock()
    page.id = uuid4()
    page.slug = "test-page"
    page.title = "Test Page"
    page.content = "Test content"
    page.is_published = True
    page.published_at = datetime.now(UTC)
    page.publish_at = None
    page.user_id = uuid4()
    page.order = 0
    page.meta_description = None
    page.og_title = None
    page.og_description = None
    page.og_image = None
    page.meta_robots = None
    return page


class TestPublishedFilter:
    """Tests for published_filter()."""

    def test_returns_two_clauses(self):
        """published_filter should return exactly 2 filter clauses."""
        clauses = published_filter()
        assert len(clauses) == 2

    def test_returns_list_of_column_elements(self):
        """published_filter should return a list of SQLAlchemy column elements."""
        clauses = published_filter()
        assert isinstance(clauses, list)
        # Each clause should be a SQLAlchemy ColumnElement (BinaryExpression or BooleanClauseList)
        for clause in clauses:
            assert hasattr(clause, "compile"), (
                "Each clause should be a compilable SQLAlchemy expression"
            )


class TestApplyFieldUpdates:
    """Tests for _apply_field_updates()."""

    def test_applies_string_value(self):
        """_apply_field_updates should set a string attribute on the page."""
        page = MagicMock()
        _apply_field_updates(page, {"title": "New Title"})
        assert page.title == "New Title"

    def test_applies_multiple_values(self):
        """_apply_field_updates should apply all provided fields."""
        page = MagicMock()
        _apply_field_updates(page, {"title": "New Title", "slug": "new-slug"})
        assert page.title == "New Title"
        assert page.slug == "new-slug"

    def test_skips_unset_values(self):
        """_apply_field_updates should skip fields with _UNSET sentinel."""
        page = MagicMock()
        page.title = "Original"
        _apply_field_updates(page, {"title": _UNSET, "slug": "new-slug"})
        # title should remain unchanged (still the original MagicMock attribute)
        assert page.title == "Original"
        assert page.slug == "new-slug"

    def test_applies_none_for_nullable_fields(self):
        """_apply_field_updates should set None on nullable fields (not skip them)."""
        page = MagicMock()
        page.meta_description = "Old description"
        _apply_field_updates(page, {"meta_description": None})
        assert page.meta_description is None

    def test_applies_boolean_false(self):
        """_apply_field_updates should apply False values (not treat as skip)."""
        page = MagicMock()
        _apply_field_updates(page, {"is_published": False})
        assert page.is_published is False

    def test_applies_integer_zero(self):
        """_apply_field_updates should apply 0 values (not treat as skip)."""
        page = MagicMock()
        _apply_field_updates(page, {"order": 0})
        assert page.order == 0

    def test_empty_dict_changes_nothing(self):
        """_apply_field_updates with empty dict should not call setattr."""
        page = MagicMock(spec=[])
        _apply_field_updates(page, {})
        # No attributes should have been set; MagicMock(spec=[]) has no attrs


class TestListPages:
    """Tests for list_pages()."""

    @pytest.mark.asyncio
    async def test_basic_call(self, mock_db_session):
        """list_pages with defaults should execute a query and return a list."""
        result = await list_pages(mock_db_session)
        assert isinstance(result, list)
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_pages_from_result(self, mock_db_session):
        """list_pages should return all pages from the database result."""
        page1 = MagicMock()
        page2 = MagicMock()

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [page1, page2]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        result = await list_pages(mock_db_session)
        assert len(result) == 2
        assert result[0] is page1
        assert result[1] is page2

    @pytest.mark.asyncio
    async def test_published_only_filter(self, mock_db_session):
        """list_pages with published_only=True should apply published filters."""
        with patch("skrift.db.services.page_service.published_filter") as mock_pf:
            # Return real SQLAlchemy expressions so and_() does not reject them
            from skrift.db.models import Page
            mock_pf.return_value = [Page.is_published == True, Page.publish_at.is_(None)]
            await list_pages(mock_db_session, published_only=True)
            mock_pf.assert_called_once()

    @pytest.mark.asyncio
    async def test_published_only_false_no_filter(self, mock_db_session):
        """list_pages with published_only=False should not call published_filter."""
        with patch("skrift.db.services.page_service.published_filter") as mock_pf:
            await list_pages(mock_db_session, published_only=False)
            mock_pf.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_id_filter(self, mock_db_session):
        """list_pages with a user_id should include the user_id in the query."""
        user_id = uuid4()
        result = await list_pages(mock_db_session, user_id=user_id)
        assert isinstance(result, list)
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_order_by_order(self, mock_db_session):
        """list_pages with order_by='order' should execute without error."""
        await list_pages(mock_db_session, order_by="order")
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_order_by_created(self, mock_db_session):
        """list_pages with order_by='created' should execute without error."""
        await list_pages(mock_db_session, order_by="created")
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_order_by_published(self, mock_db_session):
        """list_pages with order_by='published' should execute without error."""
        await list_pages(mock_db_session, order_by="published")
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_order_by_title(self, mock_db_session):
        """list_pages with order_by='title' should execute without error."""
        await list_pages(mock_db_session, order_by="title")
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pagination_with_limit(self, mock_db_session):
        """list_pages with limit should execute successfully."""
        await list_pages(mock_db_session, limit=10)
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pagination_with_offset(self, mock_db_session):
        """list_pages with offset should execute successfully."""
        await list_pages(mock_db_session, offset=5)
        mock_db_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pagination_with_limit_and_offset(self, mock_db_session):
        """list_pages with both limit and offset should execute successfully."""
        await list_pages(mock_db_session, limit=10, offset=20)
        mock_db_session.execute.assert_awaited_once()


class TestGetPageBySlug:
    """Tests for get_page_by_slug()."""

    @pytest.mark.asyncio
    async def test_found(self, mock_db_session, mock_page):
        """get_page_by_slug should return the page when found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_page
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        result = await get_page_by_slug(mock_db_session, "test-page")
        assert result is mock_page

    @pytest.mark.asyncio
    async def test_not_found(self, mock_db_session):
        """get_page_by_slug should return None when page is not found."""
        result = await get_page_by_slug(mock_db_session, "nonexistent-slug")
        assert result is None

    @pytest.mark.asyncio
    async def test_published_only(self, mock_db_session, mock_page):
        """get_page_by_slug with published_only should apply published filters."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_page
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        with patch("skrift.db.services.page_service.published_filter") as mock_pf:
            # Return real SQLAlchemy expressions so query.where() does not reject them
            from skrift.db.models import Page
            mock_pf.return_value = [Page.is_published == True, Page.publish_at.is_(None)]
            result = await get_page_by_slug(
                mock_db_session, "test-page", published_only=True
            )
            mock_pf.assert_called_once()

    @pytest.mark.asyncio
    async def test_published_only_false_no_filter(self, mock_db_session):
        """get_page_by_slug without published_only should not use published filters."""
        with patch("skrift.db.services.page_service.published_filter") as mock_pf:
            await get_page_by_slug(mock_db_session, "test-page", published_only=False)
            mock_pf.assert_not_called()


class TestGetPageById:
    """Tests for get_page_by_id()."""

    @pytest.mark.asyncio
    async def test_found(self, mock_db_session, mock_page):
        """get_page_by_id should return the page when found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_page
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        result = await get_page_by_id(mock_db_session, mock_page.id)
        assert result is mock_page

    @pytest.mark.asyncio
    async def test_not_found(self, mock_db_session):
        """get_page_by_id should return None when page is not found."""
        result = await get_page_by_id(mock_db_session, uuid4())
        assert result is None


class TestCreatePage:
    """Tests for create_page()."""

    @pytest.mark.asyncio
    async def test_basic_creation(self, mock_db_session):
        """create_page should create a Page, add it to the session, and commit."""
        with patch("skrift.db.services.page_service.Page") as MockPage, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_hooks.do_action = AsyncMock()
            mock_instance = MagicMock()
            MockPage.return_value = mock_instance

            result = await create_page(
                mock_db_session,
                slug="new-page",
                title="New Page",
                content="Some content",
            )

            # Page constructor called with correct arguments
            MockPage.assert_called_once_with(
                slug="new-page",
                title="New Page",
                content="Some content",
                is_published=False,
                published_at=None,
                user_id=None,
                order=0,
                publish_at=None,
                meta_description=None,
                og_title=None,
                og_description=None,
                og_image=None,
                meta_robots=None,
            )

            # Session interactions
            mock_db_session.add.assert_called_once_with(mock_instance)
            mock_db_session.commit.assert_awaited_once()
            mock_db_session.refresh.assert_awaited_once_with(mock_instance)

            assert result is mock_instance

    @pytest.mark.asyncio
    async def test_fires_hooks(self, mock_db_session):
        """create_page should fire BEFORE_PAGE_SAVE and AFTER_PAGE_SAVE hooks."""
        with patch("skrift.db.services.page_service.Page") as MockPage, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_hooks.do_action = AsyncMock()
            mock_instance = MagicMock()
            MockPage.return_value = mock_instance

            await create_page(
                mock_db_session,
                slug="hook-page",
                title="Hook Page",
            )

            # Should have been called twice: before and after save
            assert mock_hooks.do_action.await_count == 2

            calls = mock_hooks.do_action.await_args_list
            # First call: BEFORE_PAGE_SAVE with is_new=True
            assert calls[0].args[0] == "before_page_save"
            assert calls[0].args[1] is mock_instance
            assert calls[0].kwargs["is_new"] is True

            # Second call: AFTER_PAGE_SAVE with is_new=True
            assert calls[1].args[0] == "after_page_save"
            assert calls[1].args[1] is mock_instance
            assert calls[1].kwargs["is_new"] is True

    @pytest.mark.asyncio
    async def test_creation_with_all_fields(self, mock_db_session):
        """create_page with all optional fields should pass them to the Page."""
        user_id = uuid4()
        now = datetime.now(UTC)

        with patch("skrift.db.services.page_service.Page") as MockPage, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_hooks.do_action = AsyncMock()
            MockPage.return_value = MagicMock()

            await create_page(
                mock_db_session,
                slug="full-page",
                title="Full Page",
                content="Full content",
                is_published=True,
                published_at=now,
                user_id=user_id,
                order=5,
                publish_at=now,
                meta_description="SEO desc",
                og_title="OG Title",
                og_description="OG Desc",
                og_image="https://example.com/img.jpg",
                meta_robots="noindex",
            )

            MockPage.assert_called_once_with(
                slug="full-page",
                title="Full Page",
                content="Full content",
                is_published=True,
                published_at=now,
                user_id=user_id,
                order=5,
                publish_at=now,
                meta_description="SEO desc",
                og_title="OG Title",
                og_description="OG Desc",
                og_image="https://example.com/img.jpg",
                meta_robots="noindex",
            )


class TestUpdatePage:
    """Tests for update_page()."""

    @pytest.mark.asyncio
    async def test_page_not_found_returns_none(self, mock_db_session):
        """update_page should return None if the page does not exist."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            result = await update_page(mock_db_session, uuid4(), title="New Title")
            assert result is None

    @pytest.mark.asyncio
    async def test_updates_fields(self, mock_db_session, mock_page):
        """update_page should apply field updates to the page."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            result = await update_page(
                mock_db_session,
                mock_page.id,
                title="Updated Title",
                slug="updated-slug",
            )

            assert result is mock_page
            mock_db_session.commit.assert_awaited_once()
            mock_db_session.refresh.assert_awaited_once_with(mock_page)

    @pytest.mark.asyncio
    async def test_creates_revision_when_content_changes(self, mock_db_session, mock_page):
        """update_page should create a revision when content actually changes."""
        mock_page.content = "Old content"
        mock_page.title = "Old Title"

        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            await update_page(
                mock_db_session,
                mock_page.id,
                content="New content",
            )

            mock_rev.create_revision.assert_awaited_once_with(
                mock_db_session, mock_page, None
            )

    @pytest.mark.asyncio
    async def test_creates_revision_when_title_changes(self, mock_db_session, mock_page):
        """update_page should create a revision when title actually changes."""
        mock_page.title = "Old Title"
        mock_page.content = "Same content"

        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            await update_page(
                mock_db_session,
                mock_page.id,
                title="New Title",
            )

            mock_rev.create_revision.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_revision_when_content_unchanged(self, mock_db_session, mock_page):
        """update_page should not create a revision when content is the same."""
        mock_page.content = "Same content"
        mock_page.title = "Same Title"

        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            # Pass the same content and title values
            await update_page(
                mock_db_session,
                mock_page.id,
                content="Same content",
                title="Same Title",
            )

            mock_rev.create_revision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_revision_when_only_non_content_fields_change(self, mock_db_session, mock_page):
        """update_page should not create a revision when only non-content fields change."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            # Only change order and slug (no title/content)
            await update_page(
                mock_db_session,
                mock_page.id,
                slug="new-slug",
                order=5,
            )

            mock_rev.create_revision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_revision_when_create_revision_false(self, mock_db_session, mock_page):
        """update_page with create_revision=False should skip revision creation."""
        mock_page.content = "Old content"

        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            await update_page(
                mock_db_session,
                mock_page.id,
                content="New content",
                create_revision=False,
            )

            mock_rev.create_revision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fires_hooks(self, mock_db_session, mock_page):
        """update_page should fire BEFORE_PAGE_SAVE and AFTER_PAGE_SAVE with is_new=False."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            await update_page(mock_db_session, mock_page.id, slug="updated")

            assert mock_hooks.do_action.await_count == 2

            calls = mock_hooks.do_action.await_args_list
            # Before save
            assert calls[0].args[0] == "before_page_save"
            assert calls[0].args[1] is mock_page
            assert calls[0].kwargs["is_new"] is False

            # After save
            assert calls[1].args[0] == "after_page_save"
            assert calls[1].args[1] is mock_page
            assert calls[1].kwargs["is_new"] is False

    @pytest.mark.asyncio
    async def test_passes_user_id_to_revision(self, mock_db_session, mock_page):
        """update_page should pass user_id to revision_service.create_revision."""
        mock_page.content = "Old content"
        user_id = uuid4()

        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks, \
             patch("skrift.db.services.page_service.revision_service") as mock_rev:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()
            mock_rev.create_revision = AsyncMock()

            await update_page(
                mock_db_session,
                mock_page.id,
                content="New content",
                user_id=user_id,
            )

            mock_rev.create_revision.assert_awaited_once_with(
                mock_db_session, mock_page, user_id
            )


class TestDeletePage:
    """Tests for delete_page()."""

    @pytest.mark.asyncio
    async def test_success(self, mock_db_session, mock_page):
        """delete_page should return True when the page is found and deleted."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()

            result = await delete_page(mock_db_session, mock_page.id)

            assert result is True
            mock_db_session.delete.assert_awaited_once_with(mock_page)
            mock_db_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found(self, mock_db_session):
        """delete_page should return False when the page does not exist."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_get.return_value = None
            mock_hooks.do_action = AsyncMock()

            result = await delete_page(mock_db_session, uuid4())

            assert result is False
            mock_db_session.delete.assert_not_awaited()
            mock_db_session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fires_hooks(self, mock_db_session, mock_page):
        """delete_page should fire BEFORE_PAGE_DELETE and AFTER_PAGE_DELETE hooks."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_get.return_value = mock_page
            mock_hooks.do_action = AsyncMock()

            await delete_page(mock_db_session, mock_page.id)

            assert mock_hooks.do_action.await_count == 2

            calls = mock_hooks.do_action.await_args_list
            # Before delete
            assert calls[0].args[0] == "before_page_delete"
            assert calls[0].args[1] is mock_page

            # After delete
            assert calls[1].args[0] == "after_page_delete"
            assert calls[1].args[1] is mock_page

    @pytest.mark.asyncio
    async def test_no_hooks_when_not_found(self, mock_db_session):
        """delete_page should not fire any hooks when the page is not found."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get, \
             patch("skrift.db.services.page_service.hooks") as mock_hooks:
            mock_get.return_value = None
            mock_hooks.do_action = AsyncMock()

            await delete_page(mock_db_session, uuid4())

            mock_hooks.do_action.assert_not_awaited()


class TestCheckPageOwnership:
    """Tests for check_page_ownership()."""

    @pytest.mark.asyncio
    async def test_owns_page(self, mock_db_session, mock_page):
        """check_page_ownership should return True when user owns the page."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_page

            result = await check_page_ownership(
                mock_db_session, mock_page.id, mock_page.user_id
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_does_not_own_page(self, mock_db_session, mock_page):
        """check_page_ownership should return False when user does not own the page."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_page
            different_user = uuid4()

            result = await check_page_ownership(
                mock_db_session, mock_page.id, different_user
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_page_not_found(self, mock_db_session):
        """check_page_ownership should return False when the page does not exist."""
        with patch("skrift.db.services.page_service.get_page_by_id", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            result = await check_page_ownership(
                mock_db_session, uuid4(), uuid4()
            )
            assert result is False
