"""Site settings admin controller."""

from __future__ import annotations

from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse, Redirect
from litestar.params import Body
from litestar.enums import RequestEncodingType
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard, Permission
from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.db.services import setting_service
from skrift.lib.flash import flash_success, get_flash_messages


class SettingsAdminController(Controller):
    """Controller for site settings in admin."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/settings",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("modify-site")],
        opt={"label": "Settings", "icon": "settings", "order": 100},
    )
    async def site_settings(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Site settings page."""
        ctx = await get_admin_context(request, db_session)
        site_settings = await setting_service.get_site_settings(db_session)

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/settings/site.html",
            context={"flash_messages": flash_messages, "settings": site_settings, **ctx},
        )

    @post(
        "/settings",
        guards=[auth_guard, Permission("modify-site")],
    )
    async def save_site_settings(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Save site settings."""
        site_name = data.get("site_name", "").strip()
        site_tagline = data.get("site_tagline", "").strip()
        site_copyright_holder = data.get("site_copyright_holder", "").strip()
        site_copyright_start_year = data.get("site_copyright_start_year", "").strip()

        await setting_service.set_setting(
            db_session, setting_service.SITE_NAME_KEY, site_name
        )
        await setting_service.set_setting(
            db_session, setting_service.SITE_TAGLINE_KEY, site_tagline
        )
        await setting_service.set_setting(
            db_session, setting_service.SITE_COPYRIGHT_HOLDER_KEY, site_copyright_holder
        )
        await setting_service.set_setting(
            db_session, setting_service.SITE_COPYRIGHT_START_YEAR_KEY, site_copyright_start_year
        )

        await setting_service.load_site_settings_cache(db_session)

        flash_success(request, "Site settings saved successfully")
        return Redirect(path="/admin/settings")
