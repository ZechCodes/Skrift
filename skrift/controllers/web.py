from pathlib import Path
from uuid import UUID

from litestar import Controller, Request, get
from litestar.exceptions import NotFoundException
from litestar.response import Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User
from skrift.db.services import page_service
from skrift.lib.template import Template

TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates"


class WebController(Controller):
    path = "/"

    async def _get_user_context(
        self, request: "Request", db_session: AsyncSession
    ) -> dict:
        """Get user data for template context if logged in."""
        user_id = request.session.get("user_id")
        if not user_id:
            return {"user": None}

        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        user = result.scalar_one_or_none()
        return {"user": user}

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

        # Split path into slugs (e.g., "services/web" -> ["services", "web"])
        slugs = [s for s in path.split("/") if s]

        # Use the full path as the slug for database lookup
        page_slug = "/".join(slugs)

        # Fetch page from database
        page = await page_service.get_page_by_slug(
            db_session, page_slug, published_only=not request.session.get("user_id")
        )
        if not page:
            raise NotFoundException(f"Page '{path}' not found")

        template = Template("page", *slugs, context={"path": path, "slugs": slugs, "page": page})
        return template.render(TEMPLATE_DIR, flash=flash, **user_ctx)
