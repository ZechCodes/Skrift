from pathlib import Path
from uuid import UUID

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.db.services import page_service
from skrift.db.services.setting_service import get_cached_site_name, get_cached_site_base_url, get_cached_site_theme
from skrift.lib.hooks import RESOLVE_THEME, apply_filters
from skrift.lib.seo import get_page_seo_meta, get_page_og_meta
from skrift.lib.template import Template

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


class WebController(Controller):
    path = "/"

    async def _get_user_context(
        self, request: "Request", db_session: AsyncSession
    ) -> dict:
        """Get user data for template context if logged in."""
        user_id = request.session.get(SESSION_USER_ID)
        if not user_id:
            return {"user": None}

        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        user = result.scalar_one_or_none()
        return {"user": user}

    async def _resolve_theme(self, request: "Request") -> str:
        """Resolve the active theme for this request via filter hook."""
        theme_name = get_cached_site_theme()
        return await apply_filters(RESOLVE_THEME, theme_name, request)

    @get("/")
    async def index(
        self, request: "Request", db_session: AsyncSession
    ) -> TemplateResponse:
        """Home page."""
        user_ctx = await self._get_user_context(request, db_session)
        flash = request.session.pop("flash", None)

        return TemplateResponse(
            "index.html",
            context={"flash": flash, **user_ctx},
        )

    @get("/{path:path}")
    async def view_page(
        self, request: "Request", db_session: AsyncSession, path: str
    ) -> TemplateResponse:
        """View a page by path with WP-like template resolution."""
        user_ctx = await self._get_user_context(request, db_session)
        flash = request.session.pop("flash", None)
        theme_name = await self._resolve_theme(request)

        # Split path into slugs (e.g., "services/web" -> ["services", "web"])
        slugs = [s for s in path.split("/") if s]

        # Use the full path as the slug for database lookup
        page_slug = "/".join(slugs)

        # Fetch page from database
        page = await page_service.get_page_by_slug(
            db_session, page_slug,
            published_only=not request.session.get(SESSION_USER_ID),
            page_type="page",
        )
        if not page:
            raise NotFoundException(f"Page '{path}' not found")

        # Get SEO metadata
        site_name = get_cached_site_name()
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
        seo_meta = await get_page_seo_meta(page, site_name, base_url)
        og_meta = await get_page_og_meta(page, site_name, base_url)

        template = Template(
            "page", *slugs,
            context={
                "path": path,
                "slugs": slugs,
                "page": page,
                "seo_meta": seo_meta,
                "og_meta": og_meta,
            }
        )
        return template.render(TEMPLATE_DIR, theme_name=theme_name, flash=flash, **user_ctx)
