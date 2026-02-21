"""Dynamic controller factory for public-facing page type routes."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Response, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.config import PageTypeConfig
from skrift.db.models.user import User
from skrift.db.services import page_service
from skrift.db.services.setting_service import (
    get_cached_site_name,
    get_cached_site_base_url,
    get_cached_site_theme,
)
from skrift.lib.hooks import RESOLVE_THEME, apply_filters
from skrift.lib.seo import get_page_seo_meta, get_page_og_meta
from skrift.lib.template import Template

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


def create_public_page_type_controller(
    page_type: PageTypeConfig,
    for_subdomain: bool = False,
) -> type[Controller]:
    """Create a public Controller subclass for a specific page type.

    Primary domain mode (for_subdomain=False):
        Routes under /{type_name}, e.g. /post/hello-world

    Subdomain mode (for_subdomain=True):
        Routes under /, e.g. blog.example.com/hello-world
    """
    type_name = page_type.name
    plural = page_type.plural
    label = type_name.title()
    base_path = "/" if for_subdomain else f"/{type_name}"

    page_type_ctx = {
        "page_type_name": type_name,
        "page_type_plural": plural,
    }

    class _PublicPageTypeController(Controller):
        path = base_path

        async def _get_user_context(
            self, request: Request, db_session: AsyncSession
        ) -> dict:
            user_id = request.session.get(SESSION_USER_ID)
            if not user_id:
                return {"user": None}

            result = await db_session.execute(
                select(User).where(User.id == UUID(user_id))
            )
            user = result.scalar_one_or_none()
            return {"user": user}

        async def _resolve_theme(self, request: Request) -> str:
            theme_name = get_cached_site_theme()
            return await apply_filters(RESOLVE_THEME, theme_name, request)

        @get("/")
        async def list_pages(
            self, request: Request, db_session: AsyncSession
        ) -> TemplateResponse:
            user_ctx = await self._get_user_context(request, db_session)
            flash = request.session.pop("flash", None)
            theme_name = await self._resolve_theme(request)

            pages = await page_service.list_pages(
                db_session,
                published_only=not request.session.get(SESSION_USER_ID),
                page_type=type_name,
                order_by="published",
            )

            if for_subdomain:
                template = Template("index", context={"pages": pages})
            else:
                template = Template(
                    "archive", type_name,
                    context={"pages": pages},
                )

            return template.render(
                TEMPLATE_DIR,
                theme_name=theme_name,
                flash=flash,
                **page_type_ctx,
                **user_ctx,
            )

        @get("/{slug:str}")
        async def view_page(
            self, request: Request, db_session: AsyncSession, slug: str
        ) -> TemplateResponse | Response:
            page = await page_service.get_page_by_slug(
                db_session,
                slug,
                published_only=not request.session.get(SESSION_USER_ID),
                page_type=type_name,
            )
            if not page:
                raise NotFoundException(f"{label} '{slug}' not found")

            if "text/markdown" in request.headers.get("accept", ""):
                return Response(
                    content=page.content,
                    media_type="text/markdown",
                )

            user_ctx = await self._get_user_context(request, db_session)
            flash = request.session.pop("flash", None)
            theme_name = await self._resolve_theme(request)

            site_name = get_cached_site_name()
            base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
            seo_meta = await get_page_seo_meta(page, site_name, base_url)
            og_meta = await get_page_og_meta(page, site_name, base_url)

            template = Template(
                type_name, slug,
                context={
                    "page": page,
                    "seo_meta": seo_meta,
                    "og_meta": og_meta,
                },
            )
            return template.render(
                TEMPLATE_DIR,
                theme_name=theme_name,
                flash=flash,
                **page_type_ctx,
                **user_ctx,
            )

    suffix = "Subdomain" if for_subdomain else "Public"
    _PublicPageTypeController.__name__ = f"{label}{suffix}Controller"
    _PublicPageTypeController.__qualname__ = f"{label}{suffix}Controller"

    return _PublicPageTypeController
