"""Tests for shared public page rendering helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestWantsMarkdownResponse:
    def test_detects_markdown_accept_header(self):
        from skrift.controllers.page_rendering import wants_markdown_response

        request = MagicMock()
        request.headers = {"accept": "text/html, text/markdown"}

        assert wants_markdown_response(request) is True

    def test_ignores_other_accept_headers(self):
        from skrift.controllers.page_rendering import wants_markdown_response

        request = MagicMock()
        request.headers = {"accept": "text/html"}

        assert wants_markdown_response(request) is False


class TestBuildPublicPageRenderContext:
    @pytest.mark.asyncio
    async def test_reuses_asset_urls_for_featured_image(self):
        from skrift.controllers.page_rendering import build_public_page_render_context

        featured_asset = SimpleNamespace(id="asset-1", store="default", key="featured")
        page = SimpleNamespace(
            assets=[featured_asset],
            featured_asset=featured_asset,
        )
        request = MagicMock()
        request.session = {"flash": ["ok"]}
        request.base_url = "https://example.com/"
        request.app.state.storage_manager = MagicMock()
        db_session = AsyncMock()

        with (
            patch(
                "skrift.controllers.page_rendering.get_user_context",
                new_callable=AsyncMock,
                return_value={"user": None},
            ),
            patch(
                "skrift.controllers.page_rendering.resolve_theme",
                new_callable=AsyncMock,
                return_value="theme",
            ),
            patch(
                "skrift.controllers.page_rendering.get_asset_url",
                new_callable=AsyncMock,
                return_value="https://cdn.example.com/featured.jpg",
            ) as mock_get_asset_url,
            patch(
                "skrift.controllers.page_rendering.get_page_seo_meta",
                new_callable=AsyncMock,
                return_value={"title": "SEO"},
            ),
            patch(
                "skrift.controllers.page_rendering.get_page_og_meta",
                new_callable=AsyncMock,
                return_value={"title": "OG"},
            ) as mock_get_og_meta,
            patch("skrift.controllers.page_rendering.get_cached_site_name", return_value="Site"),
            patch(
                "skrift.controllers.page_rendering.get_cached_site_base_url",
                return_value="https://example.com",
            ),
        ):
            render_ctx = await build_public_page_render_context(
                request,
                db_session,
                page,
                include_asset_urls=True,
            )

        assert render_ctx.asset_urls == {
            "asset-1": "https://cdn.example.com/featured.jpg"
        }
        assert render_ctx.featured_image_url == "https://cdn.example.com/featured.jpg"
        assert request.session == {}
        assert mock_get_asset_url.await_count == 1
        assert mock_get_og_meta.await_args.kwargs["featured_image_url"] == (
            "https://cdn.example.com/featured.jpg"
        )
