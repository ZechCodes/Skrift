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
        from skrift.config import get_settings as get_app_settings
        from skrift.lib.theme import themes_available, discover_themes

        ctx = await get_admin_context(request, db_session)
        app_settings = get_app_settings()

        # Build sites list when multi-site is configured
        sites_list: list[dict[str, str]] = []
        if app_settings.sites and app_settings.domain:
            sites_list.append({"key": "", "label": f"{app_settings.domain} (primary)"})
            for name, site_cfg in app_settings.sites.items():
                sites_list.append({
                    "key": site_cfg.subdomain,
                    "label": f"{site_cfg.subdomain}.{app_settings.domain}",
                })

        selected_site = request.query_params.get("site", "") if sites_list else ""

        # Fetch settings — per-subdomain when a site is selected, global otherwise
        if selected_site:
            current_settings = await setting_service.get_site_settings_for_subdomain(
                db_session, selected_site
            )
        else:
            current_settings = await setting_service.get_site_settings(db_session)

        # Build theme data only for primary domain
        theme_data = None
        if not selected_site and themes_available():
            theme_data = {
                "themes": discover_themes(),
                "active": current_settings.get(setting_service.SITE_THEME_KEY, ""),
            }

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/settings/site.html",
            context={
                "flash_messages": flash_messages,
                "settings": current_settings,
                "theme_data": theme_data,
                "sites_list": sites_list,
                "selected_site": selected_site,
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
        from skrift.app_factory import update_template_directories
        from skrift.lib.theme import themes_available

        subdomain = data.get("_site", "").strip()
        site_name = data.get("site_name", "").strip()
        site_tagline = data.get("site_tagline", "").strip()

        if subdomain:
            # Per-subdomain: only save site_name and site_tagline
            await setting_service.set_site_setting_for_subdomain(
                db_session, subdomain, setting_service.SITE_NAME_KEY, site_name
            )
            await setting_service.set_site_setting_for_subdomain(
                db_session, subdomain, setting_service.SITE_TAGLINE_KEY, site_tagline
            )
        else:
            # Primary domain: save all fields
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

            # Update template directories for instant theme switching
            update_template_directories()

        await setting_service.load_site_settings_cache(db_session)

        flash_success(request, "Site settings saved successfully")
        redirect_path = "/admin/settings"
        if subdomain:
            redirect_path += f"?site={subdomain}"
        return Redirect(path=redirect_path)

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
