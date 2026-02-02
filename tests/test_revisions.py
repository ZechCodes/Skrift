"""Tests for the page revision service."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4


class TestRevisionService:
    """Test the revision_service functions."""

    @pytest.fixture
    def mock_page(self):
        """Create a mock page."""
        page = MagicMock()
        page.id = uuid4()
        page.title = "Original Title"
        page.content = "Original content"
        return page

    @pytest.fixture
    def mock_revision(self):
        """Create a mock revision."""
        revision = MagicMock()
        revision.id = uuid4()
        revision.page_id = uuid4()
        revision.revision_number = 1
        revision.title = "Old Title"
        revision.content = "Old content"
        return revision

    @pytest.mark.asyncio
    async def test_create_revision_snapshots_content(self, mock_page):
        """Test that create_revision captures current page state."""
        with patch("skrift.db.services.revision_service.PageRevision") as MockRevision:
            from skrift.db.services.revision_service import create_revision

            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0)))
            mock_session.commit = AsyncMock()
            mock_session.refresh = AsyncMock()

            # Create revision should capture title and content
            await create_revision(mock_session, mock_page, user_id=None)

            # Verify PageRevision was created with correct values
            call_kwargs = MockRevision.call_args[1]
            assert call_kwargs["title"] == "Original Title"
            assert call_kwargs["content"] == "Original content"
            assert call_kwargs["page_id"] == mock_page.id

    @pytest.mark.asyncio
    async def test_revision_number_increments(self, mock_page):
        """Test that revision numbers increment properly."""
        with patch("skrift.db.services.revision_service.PageRevision") as MockRevision:
            from skrift.db.services.revision_service import create_revision

            mock_session = AsyncMock()
            # Simulate existing max revision number of 5
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=5)))
            mock_session.commit = AsyncMock()
            mock_session.refresh = AsyncMock()

            await create_revision(mock_session, mock_page, user_id=None)

            # Next revision should be 6
            call_kwargs = MockRevision.call_args[1]
            assert call_kwargs["revision_number"] == 6

    @pytest.mark.asyncio
    async def test_restore_revision_updates_page(self, mock_page, mock_revision):
        """Test that restore_revision updates page with revision content."""
        from skrift.db.services.revision_service import restore_revision

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=1)))
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        # Restore should update page title and content
        with patch("skrift.db.services.revision_service.create_revision", new_callable=AsyncMock):
            result = await restore_revision(mock_session, mock_page, mock_revision, user_id=None)

        assert result.title == "Old Title"
        assert result.content == "Old content"

    @pytest.mark.asyncio
    async def test_restore_creates_new_revision_first(self, mock_page, mock_revision):
        """Test that restore creates a revision of current state first."""
        from skrift.db.services.revision_service import restore_revision

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=1)))
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        with patch("skrift.db.services.revision_service.create_revision", new_callable=AsyncMock) as mock_create:
            await restore_revision(mock_session, mock_page, mock_revision, user_id=None)

            # create_revision should be called before updating
            mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_revision_tracks_user_id(self, mock_page):
        """Test that revisions track the user who made the change."""
        with patch("skrift.db.services.revision_service.PageRevision") as MockRevision:
            from skrift.db.services.revision_service import create_revision

            mock_session = AsyncMock()
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0)))
            mock_session.commit = AsyncMock()
            mock_session.refresh = AsyncMock()

            user_id = uuid4()
            await create_revision(mock_session, mock_page, user_id=user_id)

            call_kwargs = MockRevision.call_args[1]
            assert call_kwargs["user_id"] == user_id
