"""Site settings admin controller."""

from __future__ import annotations

from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.response import File, Template as TemplateResponse, Redirect
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
        from skrift.lib.theme import themes_available, discover_themes

        ctx = await get_admin_context(request, db_session)
        site_settings = await setting_service.get_site_settings(db_session)

        # Build theme data only when themes directory exists
        theme_data = None
        if themes_available():
            theme_data = {
                "themes": discover_themes(),
                "active": site_settings.get(setting_service.SITE_THEME_KEY, ""),
            }

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/settings/site.html",
            context={
                "flash_messages": flash_messages,
                "settings": site_settings,
                "theme_data": theme_data,
                **ctx,
            },
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
        from skrift.app_factory import update_template_directories, update_static_directories
        from skrift.lib.theme import themes_available

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

        # Save theme selection if themes are available
        if themes_available():
            site_theme = data.get("site_theme", "").strip()
            await setting_service.set_setting(
                db_session, setting_service.SITE_THEME_KEY, site_theme
            )

        await setting_service.load_site_settings_cache(db_session)

        # Update template and static directories for instant theme switching
        update_template_directories()
        update_static_directories()

        flash_success(request, "Site settings saved successfully")
        return Redirect(path="/admin/settings")

    @get(
        "/theme-screenshot/{name:str}",
        guards=[auth_guard],
    )
    async def theme_screenshot(self, request: Request, name: str) -> File:
        """Serve a theme's screenshot image."""
        from litestar.response import File
        from skrift.lib.theme import get_theme_info

        info = get_theme_info(name)
        if not info or not info.screenshot:
            raise HTTPException(status_code=404, detail="Screenshot not found")

        return File(path=info.screenshot, media_type="image/png")
