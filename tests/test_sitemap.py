"""Tests for the sitemap and robots.txt controller."""

import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock, AsyncMock, patch

from skrift.controllers.sitemap import SitemapController, SitemapEntry
from skrift.lib.hooks import hooks


@pytest.fixture
def mock_page():
    """Create a mock page."""
    page = MagicMock()
    page.slug = "test-page"
    page.updated_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    page.created_at = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)
    return page


class TestSitemapEntry:
    """Test the SitemapEntry dataclass."""

    def test_sitemap_entry_required_fields(self):
        """Test SitemapEntry with only required fields."""
        entry = SitemapEntry(loc="https://example.com/page")
        assert entry.loc == "https://example.com/page"
        assert entry.lastmod is None
        assert entry.changefreq is None
        assert entry.priority is None

    def test_sitemap_entry_all_fields(self):
        """Test SitemapEntry with all fields."""
        now = datetime.now(UTC)
        entry = SitemapEntry(
            loc="https://example.com/page",
            lastmod=now,
            changefreq="weekly",
            priority=0.8,
        )
        assert entry.loc == "https://example.com/page"
        assert entry.lastmod == now
        assert entry.changefreq == "weekly"
        assert entry.priority == 0.8


class TestSitemapController:
    """Test the SitemapController class."""

    def test_build_sitemap_xml_empty(self):
        """Test building sitemap XML with no entries."""
        controller = SitemapController(owner=MagicMock())
        xml = controller._build_sitemap_xml([])

        assert b'<?xml version="1.0" encoding="UTF-8"?>' in xml
        assert b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"' in xml

    def test_build_sitemap_xml_with_entries(self):
        """Test building sitemap XML with entries."""
        controller = SitemapController(owner=MagicMock())
        entries = [
            SitemapEntry(
                loc="https://example.com/",
                lastmod=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
                changefreq="daily",
                priority=1.0,
            ),
            SitemapEntry(
                loc="https://example.com/about",
                lastmod=datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC),
                changefreq="weekly",
                priority=0.8,
            ),
        ]
        xml = controller._build_sitemap_xml(entries)

        assert b"<url>" in xml
        assert b"<loc>https://example.com/</loc>" in xml
        assert b"<loc>https://example.com/about</loc>" in xml
        assert b"<lastmod>2026-01-15</lastmod>" in xml
        assert b"<changefreq>daily</changefreq>" in xml
        assert b"<priority>1.0</priority>" in xml

    def test_build_sitemap_xml_optional_fields(self):
        """Test that optional fields are omitted when None."""
        controller = SitemapController(owner=MagicMock())
        entries = [
            SitemapEntry(loc="https://example.com/page"),
        ]
        xml = controller._build_sitemap_xml(entries)

        assert b"<loc>https://example.com/page</loc>" in xml
        assert b"<lastmod>" not in xml
        assert b"<changefreq>" not in xml
        assert b"<priority>" not in xml


class TestSitemapFilters:
    """Test sitemap filter hooks."""

    @pytest.mark.asyncio
    async def test_sitemap_page_filter_can_exclude(self, mock_page, clean_hooks):
        """Test that sitemap_page filter can exclude pages."""
        def exclude_page(entry, page):
            if page.slug == "private":
                return None
            return entry

        hooks.add_filter("sitemap_page", exclude_page)

        # The filter should work when applied
        entry = SitemapEntry(loc="https://example.com/private")
        mock_page.slug = "private"
        result = await hooks.apply_filters("sitemap_page", entry, mock_page)
        assert result is None

    @pytest.mark.asyncio
    async def test_sitemap_urls_filter_adds_custom(self, clean_hooks):
        """Test that sitemap_urls filter can add custom URLs."""
        def add_custom_urls(entries):
            entries.append(SitemapEntry(loc="https://example.com/custom"))
            return entries

        hooks.add_filter("sitemap_urls", add_custom_urls)

        entries = [SitemapEntry(loc="https://example.com/page")]
        result = await hooks.apply_filters("sitemap_urls", entries)

        assert len(result) == 2
        assert result[1].loc == "https://example.com/custom"


class TestRobotsTxtFilters:
    """Test robots.txt filter hooks."""

    @pytest.mark.asyncio
    async def test_robots_txt_filter_modifies_content(self, clean_hooks):
        """Test that robots_txt filter can modify content."""
        def add_disallow(content):
            return content + "\nDisallow: /admin/"

        hooks.add_filter("robots_txt", add_disallow)

        content = "User-agent: *\nAllow: /"
        result = await hooks.apply_filters("robots_txt", content)

        assert "Disallow: /admin/" in result
