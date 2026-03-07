"""Dynamic controller factory for public-facing page type routes."""

from __future__ import annotations

from pathlib import Path

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.config import PageTypeConfig
from skrift.controllers.helpers import get_user_context, resolve_theme
from skrift.controllers.page_rendering import (
    build_public_page_render_context,
    wants_markdown_response,
)
from skrift.db.services import page_service
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

        @get("/")
        async def list_pages(
            self, request: Request, db_session: AsyncSession
        ) -> TemplateResponse:
            user_ctx = await get_user_context(request, db_session)
            flash = request.session.pop("flash", None)
            theme_name = await resolve_theme(request)

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

            if wants_markdown_response(request):
                return Response(
                    content=page.content,
                    media_type="text/markdown",
                )

            render_ctx = await build_public_page_render_context(
                request,
                db_session,
                page,
                include_asset_urls=True,
            )

            template = Template(
                type_name, slug,
                context={
                    "page": page,
                    "seo_meta": render_ctx.seo_meta,
                    "og_meta": render_ctx.og_meta,
                    "featured_image_url": render_ctx.featured_image_url,
                    "asset_urls": render_ctx.asset_urls,
                },
            )
            return template.render(
                TEMPLATE_DIR,
                theme_name=render_ctx.theme_name,
                flash=render_ctx.flash,
                **page_type_ctx,
                **render_ctx.user_ctx,
            )

    suffix = "Subdomain" if for_subdomain else "Public"
    _PublicPageTypeController.__name__ = f"{label}{suffix}Controller"
    _PublicPageTypeController.__qualname__ = f"{label}{suffix}Controller"

    return _PublicPageTypeController
