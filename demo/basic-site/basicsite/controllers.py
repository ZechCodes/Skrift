"""Basic site controller that adds published pages to the navigation."""

from pathlib import Path
from uuid import UUID

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User
from skrift.db.services import page_service
from skrift.db.services.setting_service import get_cached_site_name, get_cached_site_base_url
from skrift.lib.seo import get_page_seo_meta, get_page_og_meta
from skrift.lib.template import Template

TEMPLATE_DIR = Path(__file__).parent.parent.parent.parent / "skrift" / "templates"


class SiteController(Controller):
    path = "/"

    async def _get_user(self, request: Request, db_session: AsyncSession) -> User | None:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        return result.scalar_one_or_none()

    async def _get_nav_pages(self, db_session: AsyncSession) -> list:
        return await page_service.list_pages(
            db_session, published_only=True, order_by="order"
        )

    @get("/")
    async def index(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash = request.session.pop("flash", None)
        nav_pages = await self._get_nav_pages(db_session)

        return TemplateResponse(
            "index.html",
            context={"user": user, "flash": flash, "nav_pages": nav_pages},
        )

    @get("/{path:path}")
    async def view_page(
        self, request: Request, db_session: AsyncSession, path: str
    ) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash = request.session.pop("flash", None)
        nav_pages = await self._get_nav_pages(db_session)

        slugs = [s for s in path.split("/") if s]
        page_slug = "/".join(slugs)

        page = await page_service.get_page_by_slug(
            db_session, page_slug, published_only=not request.session.get("user_id")
        )
        if not page:
            raise NotFoundException(f"Page '{path}' not found")

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
                "nav_pages": nav_pages,
                "seo_meta": seo_meta,
                "og_meta": og_meta,
            }
        )
        return template.render(TEMPLATE_DIR, flash=flash, user=user)
