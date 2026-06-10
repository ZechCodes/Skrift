"""Authenticated account page."""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.exceptions import NotAuthorizedException
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.controllers.helpers import get_user_context
from skrift.lib.flash import get_flash_messages
from skrift.lib.hooks import ACCOUNT_PAGE_CONTEXT, ACCOUNT_PAGE_SECTIONS, hooks


class AccountController(Controller):
    """Core account page with hook-based extension sections."""

    path = "/account"
    guards = [auth_guard]

    @get(["", "/"])
    async def account_index(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> TemplateResponse:
        user_ctx = await get_user_context(request, db_session)
        user = user_ctx.get("user")
        if user is None:
            raise NotAuthorizedException("Authentication required")

        context = {
            "request": request,
            "flash_messages": get_flash_messages(request),
            **user_ctx,
        }
        context = await hooks.apply_filters(
            ACCOUNT_PAGE_CONTEXT,
            context,
            request,
            db_session,
            user,
        )
        sections = await hooks.apply_filters(
            ACCOUNT_PAGE_SECTIONS,
            [],
            request,
            db_session,
            user,
            context,
        )
        context["account_sections"] = [
            section
            for section in sections
            if isinstance(section, dict) and section.get("template")
        ]

        return TemplateResponse("account/index.html", context=context)
