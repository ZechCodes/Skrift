"""Tests for the SEO metadata utilities."""

import pytest
from unittest.mock import MagicMock, AsyncMock

from skrift.lib.seo import (
    SEOMeta,
    OpenGraphMeta,
    get_page_seo_meta,
    get_page_og_meta,
)
from skrift.lib.hooks import hooks


@pytest.fixture
def mock_page():
    """Create a mock page with SEO fields."""
    page = MagicMock()
    page.slug = "test-page"
    page.title = "Test Page Title"
    page.meta_description = "This is a test page description"
    page.meta_robots = None
    page.og_title = None
    page.og_description = None
    page.og_image = None
    return page


@pytest.fixture
def clean_hooks():
    """Reset hooks after each test."""
    original_filters = hooks._filters.copy()
    yield
    hooks._filters = original_filters


class TestSEOMeta:
    """Test the SEOMeta dataclass."""

    def test_seo_meta_attributes(self):
        """Test SEOMeta holds all expected attributes."""
        meta = SEOMeta(
            title="Page Title",
            description="Description",
            canonical_url="https://example.com/page",
            robots="noindex",
        )
        assert meta.title == "Page Title"
        assert meta.description == "Description"
        assert meta.canonical_url == "https://example.com/page"
        assert meta.robots == "noindex"


class TestOpenGraphMeta:
    """Test the OpenGraphMeta dataclass."""

    def test_og_meta_attributes(self):
        """Test OpenGraphMeta holds all expected attributes."""
        meta = OpenGraphMeta(
            title="OG Title",
            description="OG Description",
            image="https://example.com/image.jpg",
            url="https://example.com/page",
            site_name="My Site",
            type="article",
        )
        assert meta.title == "OG Title"
        assert meta.description == "OG Description"
        assert meta.image == "https://example.com/image.jpg"
        assert meta.url == "https://example.com/page"
        assert meta.site_name == "My Site"
        assert meta.type == "article"

    def test_og_meta_default_type(self):
        """Test OpenGraphMeta default type is 'website'."""
        meta = OpenGraphMeta(
            title="Title",
            description=None,
            image=None,
            url="https://example.com",
            site_name="Site",
        )
        assert meta.type == "website"


class TestGetPageSeoMeta:
    """Test get_page_seo_meta function."""

    @pytest.mark.asyncio
    async def test_get_page_seo_meta_basic(self, mock_page, clean_hooks):
        """Test basic SEO meta generation."""
        meta = await get_page_seo_meta(mock_page, "My Site", "https://example.com")

        assert meta.title == "Test Page Title | My Site"
        assert meta.description == "This is a test page description"
        assert meta.canonical_url == "https://example.com/test-page"
        assert meta.robots is None

    @pytest.mark.asyncio
    async def test_get_page_seo_meta_without_site_name(self, mock_page, clean_hooks):
        """Test SEO meta when site name is empty."""
        meta = await get_page_seo_meta(mock_page, "", "https://example.com")

        assert meta.title == "Test Page Title"

    @pytest.mark.asyncio
    async def test_get_page_seo_meta_home_page(self, mock_page, clean_hooks):
        """Test SEO meta for home page (empty slug)."""
        mock_page.slug = ""
        meta = await get_page_seo_meta(mock_page, "My Site", "https://example.com")

        assert meta.canonical_url == "https://example.com"

    @pytest.mark.asyncio
    async def test_get_page_seo_meta_with_robots(self, mock_page, clean_hooks):
        """Test SEO meta with robots directive."""
        mock_page.meta_robots = "noindex, nofollow"
        meta = await get_page_seo_meta(mock_page, "My Site", "https://example.com")

        assert meta.robots == "noindex, nofollow"

    @pytest.mark.asyncio
    async def test_get_page_seo_meta_with_filter(self, mock_page, clean_hooks):
        """Test that page_seo_meta filter modifies the result."""
        def modify_meta(meta, page, site_name, base_url):
            meta.title = "Modified Title"
            return meta

        hooks.add_filter("page_seo_meta", modify_meta)

        meta = await get_page_seo_meta(mock_page, "My Site", "https://example.com")

        assert meta.title == "Modified Title"


class TestGetPageOgMeta:
    """Test get_page_og_meta function."""

    @pytest.mark.asyncio
    async def test_get_page_og_meta_basic(self, mock_page, clean_hooks):
        """Test basic OpenGraph meta generation."""
        meta = await get_page_og_meta(mock_page, "My Site", "https://example.com")

        assert meta.title == "Test Page Title"  # Falls back to page title
        assert meta.description == "This is a test page description"  # Falls back to meta_description
        assert meta.url == "https://example.com/test-page"
        assert meta.site_name == "My Site"
        assert meta.image is None

    @pytest.mark.asyncio
    async def test_get_page_og_meta_fallback_to_title(self, mock_page, clean_hooks):
        """Test OG meta falls back to page title when og_title is None."""
        mock_page.og_title = None
        meta = await get_page_og_meta(mock_page, "My Site", "https://example.com")

        assert meta.title == "Test Page Title"

    @pytest.mark.asyncio
    async def test_get_page_og_meta_custom_og_title(self, mock_page, clean_hooks):
        """Test OG meta uses og_title when set."""
        mock_page.og_title = "Custom OG Title"
        meta = await get_page_og_meta(mock_page, "My Site", "https://example.com")

        assert meta.title == "Custom OG Title"

    @pytest.mark.asyncio
    async def test_og_image_included_when_set(self, mock_page, clean_hooks):
        """Test that og_image is included when set."""
        mock_page.og_image = "https://example.com/image.jpg"
        meta = await get_page_og_meta(mock_page, "My Site", "https://example.com")

        assert meta.image == "https://example.com/image.jpg"

    @pytest.mark.asyncio
    async def test_get_page_og_meta_with_filter(self, mock_page, clean_hooks):
        """Test that page_og_meta filter modifies the result."""
        def modify_og(meta, page, site_name, base_url):
            meta.type = "article"
            return meta

        hooks.add_filter("page_og_meta", modify_og)

        meta = await get_page_og_meta(mock_page, "My Site", "https://example.com")

        assert meta.type == "article"

    @pytest.mark.asyncio
    async def test_canonical_url_generation_with_trailing_slash(self, mock_page, clean_hooks):
        """Test canonical URL generation handles trailing slashes correctly."""
        meta = await get_page_seo_meta(mock_page, "My Site", "https://example.com/")

        assert meta.canonical_url == "https://example.com/test-page"
