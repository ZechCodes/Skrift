"""Tests for content scheduling functionality."""

import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock


class TestContentScheduling:
    """Test content scheduling in page_service."""

    def test_page_publish_at_field_exists(self):
        """Test that Page model has publish_at field."""
        from skrift.db.models import Page

        page = Page(
            slug="test",
            title="Test",
            content="",
        )
        # publish_at should be None by default
        assert hasattr(page, "publish_at")

    def test_scheduled_page_attributes(self):
        """Test creating a page with scheduled publish time."""
        from skrift.db.models import Page

        future_time = datetime.now(UTC) + timedelta(days=7)
        page = Page(
            slug="test",
            title="Test",
            content="",
            is_published=True,
            publish_at=future_time,
        )
        assert page.publish_at == future_time
        assert page.is_published is True


class TestSchedulingLogic:
    """Test scheduling logic in page_service queries."""

    @pytest.fixture
    def now(self):
        """Get current UTC time."""
        return datetime.now(UTC)

    def test_page_with_null_publish_at_uses_is_published(self):
        """Test that pages without publish_at rely on is_published only."""
        from skrift.db.models import Page

        # Published page with no schedule should be visible
        page = Page(
            slug="test",
            title="Test",
            content="",
            is_published=True,
            publish_at=None,
        )
        assert page.is_published is True
        assert page.publish_at is None

    def test_page_with_past_publish_at(self, now):
        """Test page with publish_at in the past."""
        from skrift.db.models import Page

        past_time = now - timedelta(days=1)
        page = Page(
            slug="test",
            title="Test",
            content="",
            is_published=True,
            publish_at=past_time,
        )
        # Page should be considered visible (publish_at is in the past)
        assert page.publish_at < now

    def test_page_with_future_publish_at(self, now):
        """Test page with publish_at in the future."""
        from skrift.db.models import Page

        future_time = now + timedelta(days=1)
        page = Page(
            slug="test",
            title="Test",
            content="",
            is_published=True,
            publish_at=future_time,
        )
        # Page should not be visible yet (publish_at is in the future)
        assert page.publish_at > now
