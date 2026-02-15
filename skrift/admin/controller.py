"""Admin controller — index only. Page, user, and settings controllers are in separate modules."""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.exceptions import NotAuthorizedException
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.admin.helpers import get_admin_context
from skrift.lib.flash import get_flash_messages

# Re-export split controllers for convenient registration
from skrift.admin.pages import PageAdminController  # noqa: F401
from skrift.admin.users import UserAdminController  # noqa: F401
from skrift.admin.settings import SettingsAdminController  # noqa: F401


class AdminController(Controller):
    """Controller for admin index."""

    path = "/admin"
    guards = [auth_guard]

    @get("/")
    async def admin_index(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Admin landing page. Returns 403 if user has no accessible admin pages."""
        ctx = await get_admin_context(request, db_session)

        if not ctx["admin_nav"]:
            raise NotAuthorizedException("No admin pages accessible")

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/admin.html",
            context={"flash_messages": flash_messages, **ctx},
        )
