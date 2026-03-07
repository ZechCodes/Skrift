from pathlib import Path

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Response, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.controllers.helpers import get_user_context
from skrift.controllers.page_rendering import (
    build_public_page_render_context,
    wants_markdown_response,
)
from skrift.db.services import page_service
from skrift.lib.template import Template

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


class WebController(Controller):
    path = "/"

    @get("/")
    async def index(
        self, request: "Request", db_session: AsyncSession
    ) -> TemplateResponse:
        """Home page."""
        user_ctx = await get_user_context(request, db_session)
        flash = request.session.pop("flash", None)

        return TemplateResponse(
            "index.html",
            context={"flash": flash, **user_ctx},
        )

    @get("/{path:path}")
    async def view_page(
        self, request: "Request", db_session: AsyncSession, path: str
    ) -> TemplateResponse | Response:
        """View a page by path with WP-like template resolution."""
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

        if wants_markdown_response(request):
            return Response(
                content=page.content,
                media_type="text/markdown",
            )

        render_ctx = await build_public_page_render_context(request, db_session, page)

        template = Template(
            "page", *slugs,
            context={
                "path": path,
                "slugs": slugs,
                "page": page,
                "seo_meta": render_ctx.seo_meta,
                "og_meta": render_ctx.og_meta,
                "featured_image_url": render_ctx.featured_image_url,
            }
        )
        return template.render(
            TEMPLATE_DIR,
            theme_name=render_ctx.theme_name,
            flash=render_ctx.flash,
            **render_ctx.user_ctx,
        )
