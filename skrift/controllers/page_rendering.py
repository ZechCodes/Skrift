"""Shared helpers for rendering public page responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.controllers.helpers import get_user_context, resolve_theme
from skrift.db.services.asset_service import get_asset_url, image_url, internal_asset_url
from skrift.db.services.setting_service import (
    get_cached_site_base_url,
    get_cached_site_name,
)
from skrift.seo import get_page_og_meta, get_page_seo_meta
from skrift.storage import StorageManager


@dataclass
class PublicPageRenderContext:
    """Resolved data required to render a public page template."""

    asset_urls: dict[str, str]
    asset_image_urls: dict[str, str]
    featured_image_url: str | None
    featured_cover_url: str | None
    flash: Any
    og_meta: dict[str, Any]
    seo_meta: dict[str, Any]
    theme_name: str
    user_ctx: dict[str, Any]


def wants_markdown_response(request: Request) -> bool:
    """Return whether the client explicitly requested Markdown."""
    return "text/markdown" in request.headers.get("accept", "")


async def build_public_page_render_context(
    request: Request,
    db_session: AsyncSession,
    page: Any,
    *,
    include_asset_urls: bool = False,
) -> PublicPageRenderContext:
    """Resolve shared page context used by public controllers."""
    user_ctx = await get_user_context(request, db_session)
    flash = request.session.pop("flash", None)
    theme_name = await resolve_theme(request)

    asset_urls: dict[str, str] = {}
    asset_image_urls: dict[str, str] = {}
    featured_image_url = None
    featured_cover_url = None
    storage: StorageManager | None = None

    if include_asset_urls and page.assets:
        storage = request.app.state.storage_manager
        asset_urls = {
            str(asset.id): await get_asset_url(storage, asset)
            for asset in page.assets
        }
        # Pre-resolve gallery thumbnails at the ``medium`` size. ``image_url``
        # returns the backend's direct (CDN) URL once the variant exists, so warm
        # requests skip Skrift; cold ones fall back to the lazy ``/storage`` URL.
        asset_image_urls = {
            str(asset.id): await image_url(storage, asset, "medium")
            for asset in page.assets
            if asset.content_type.startswith("image/")
        }

    if page.featured_asset:
        # The og:image tag sizes this in-template, so keep it as the
        # Skrift-internal URL that routes through the storage middleware on any
        # backend. The on-page cover is pre-resolved for the warm CDN path.
        featured_image_url = internal_asset_url(
            page.featured_asset.store, page.featured_asset.key
        )
        storage = storage or request.app.state.storage_manager
        featured_cover_url = await image_url(storage, page.featured_asset, "cover")

    site_name = get_cached_site_name()
    base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
    seo_meta = await get_page_seo_meta(page, site_name, base_url)
    og_meta = await get_page_og_meta(
        page,
        site_name,
        base_url,
        featured_image_url=featured_image_url,
    )

    return PublicPageRenderContext(
        asset_urls=asset_urls,
        asset_image_urls=asset_image_urls,
        featured_image_url=featured_image_url,
        featured_cover_url=featured_cover_url,
        flash=flash,
        og_meta=og_meta,
        seo_meta=seo_meta,
        theme_name=theme_name,
        user_ctx=user_ctx,
    )
