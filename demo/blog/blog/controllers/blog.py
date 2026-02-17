"""Blog demo controller — homepage, single post, and static pages."""

from pathlib import Path
from uuid import UUID

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User
from skrift.db.services import page_service
from skrift.db.services.setting_service import (
    get_cached_site_name,
    get_cached_site_base_url,
    get_cached_site_theme,
)
from skrift.lib.hooks import RESOLVE_THEME, apply_filters
from skrift.lib.notifications import _ensure_nid
from skrift.lib.seo import get_page_seo_meta, get_page_og_meta
from skrift.lib.template import Template

import blog.hooks  # noqa: F401 — registers hooks on import

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


class BlogController(Controller):
    path = "/"

    async def _get_user_context(
        self, request: "Request", db_session: AsyncSession
    ) -> dict:
        user_id = request.session.get("user_id")
        if not user_id:
            return {"user": None}
        result = await db_session.execute(
            select(User).where(User.id == UUID(user_id))
        )
        user = result.scalar_one_or_none()
        return {"user": user}

    async def _resolve_theme(self, request: "Request") -> str:
        theme_name = get_cached_site_theme()
        return await apply_filters(RESOLVE_THEME, theme_name, request)

    @get("/")
    async def index(
        self, request: "Request", db_session: AsyncSession
    ) -> TemplateResponse:
        """Blog homepage — list published posts."""
        user_ctx = await self._get_user_context(request, db_session)
        theme_name = await self._resolve_theme(request)
        flash = request.session.pop("flash", None)

        # Ensure notification ID for real-time updates
        nid = _ensure_nid(request)
        blog.hooks.register_blog_session(nid)

        posts = await page_service.list_pages(
            db_session,
            published_only=not request.session.get("user_id"),
            page_type="post",
            order_by="published",
        )

        nav_pages = await page_service.list_pages(
            db_session,
            published_only=True,
            page_type="page",
            order_by="order",
        )

        template = Template(
            "index",
            context={
                "posts": posts,
                "nav_pages": nav_pages,
            },
        )
        return template.render(
            TEMPLATE_DIR,
            theme_name=theme_name,
            flash=flash,
            **user_ctx,
        )

    @get("/post/{slug:str}")
    async def view_post(
        self, request: "Request", db_session: AsyncSession, slug: str
    ) -> TemplateResponse:
        """Single post view."""
        user_ctx = await self._get_user_context(request, db_session)
        theme_name = await self._resolve_theme(request)
        flash = request.session.pop("flash", None)

        nid = _ensure_nid(request)
        blog.hooks.register_blog_session(nid)

        page = await page_service.get_page_by_slug(
            db_session,
            slug,
            published_only=not request.session.get("user_id"),
            page_type="post",
        )
        if not page:
            raise NotFoundException(f"Post '{slug}' not found")

        site_name = get_cached_site_name()
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
        seo_meta = await get_page_seo_meta(page, site_name, base_url)
        og_meta = await get_page_og_meta(page, site_name, base_url)

        template = Template(
            "post",
            slug,
            context={
                "post": page,
                "page": page,
                "seo_meta": seo_meta,
                "og_meta": og_meta,
            },
        )
        return template.render(
            TEMPLATE_DIR,
            theme_name=theme_name,
            flash=flash,
            **user_ctx,
        )

    @get("/{path:path}")
    async def view_page(
        self, request: "Request", db_session: AsyncSession, path: str
    ) -> TemplateResponse:
        """Static page fallback with WP-like template resolution."""
        user_ctx = await self._get_user_context(request, db_session)
        theme_name = await self._resolve_theme(request)
        flash = request.session.pop("flash", None)

        slugs = [s for s in path.split("/") if s]
        page_slug = "/".join(slugs)

        page = await page_service.get_page_by_slug(
            db_session,
            page_slug,
            published_only=not request.session.get("user_id"),
        )
        if not page:
            raise NotFoundException(f"Page '{path}' not found")

        site_name = get_cached_site_name()
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
        seo_meta = await get_page_seo_meta(page, site_name, base_url)
        og_meta = await get_page_og_meta(page, site_name, base_url)

        template = Template(
            "page",
            *slugs,
            context={
                "path": path,
                "slugs": slugs,
                "page": page,
                "seo_meta": seo_meta,
                "og_meta": og_meta,
            },
        )
        return template.render(
            TEMPLATE_DIR,
            theme_name=theme_name,
            flash=flash,
            **user_ctx,
        )
