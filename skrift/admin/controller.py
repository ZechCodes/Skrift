"""Admin controller for administrative functionality."""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.exceptions import NotAuthorizedException
from litestar.response import Template as TemplateResponse, Redirect
from litestar.params import Body
from litestar.enums import RequestEncodingType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.guards import auth_guard, Permission
from skrift.auth.services import (
    get_user_permissions,
    assign_role_to_user,
    remove_role_from_user,
    invalidate_user_permissions_cache,
)
from skrift.auth.roles import ROLE_DEFINITIONS
from skrift.admin.navigation import build_admin_nav, ADMIN_NAV_TAG
from skrift.db.models.user import User
from skrift.db.models import Page
from skrift.db.services import page_service
from skrift.db.services import setting_service


class AdminController(Controller):
    """Controller for admin functionality."""

    path = "/admin"
    guards = [auth_guard]

    async def _get_admin_context(
        self, request: Request, db_session: AsyncSession
    ) -> dict:
        """Get common admin context including nav and user."""
        user_id = request.session.get("user_id")
        if not user_id:
            raise NotAuthorizedException("Authentication required")

        result = await db_session.execute(
            select(User).where(User.id == UUID(user_id))
        )
        user = result.scalar_one_or_none()
        if not user:
            raise NotAuthorizedException("Invalid user session")

        permissions = await get_user_permissions(db_session, user_id)
        nav_items = await build_admin_nav(
            request.app, permissions, request.url.path
        )

        return {
            "user": user,
            "permissions": permissions,
            "admin_nav": nav_items,
            "current_path": request.url.path,
        }

    @get("/")
    async def admin_index(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Admin landing page. Returns 403 if user has no accessible admin pages."""
        ctx = await self._get_admin_context(request, db_session)

        # Check if user has any admin pages accessible
        if not ctx["admin_nav"]:
            raise NotAuthorizedException("No admin pages accessible")

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/admin.html",
            context={"flash": flash, **ctx},
        )

    @get(
        "/users",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-users")],
        opt={"label": "Users", "icon": "users", "order": 10},
    )
    async def list_users(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List all users with their roles."""
        ctx = await self._get_admin_context(request, db_session)

        # Get all users with their roles
        result = await db_session.execute(
            select(User)
            .options(selectinload(User.roles))
            .order_by(User.created_at.desc())
        )
        users = list(result.scalars().all())

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/users/list.html",
            context={"flash": flash, "users": users, **ctx},
        )

    @get(
        "/users/{user_id:uuid}/roles",
        guards=[auth_guard, Permission("manage-users")],
    )
    async def edit_user_roles(
        self, request: Request, db_session: AsyncSession, user_id: UUID
    ) -> TemplateResponse:
        """Edit user roles form."""
        ctx = await self._get_admin_context(request, db_session)

        # Get the target user
        result = await db_session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles))
        )
        target_user = result.scalar_one_or_none()
        if not target_user:
            raise NotAuthorizedException("User not found")

        # Get user's current role names
        current_roles = {role.name for role in target_user.roles}

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/users/roles.html",
            context={
                "flash": flash,
                "target_user": target_user,
                "current_roles": current_roles,
                "available_roles": ROLE_DEFINITIONS,
                **ctx,
            },
        )

    @post(
        "/users/{user_id:uuid}/roles",
        guards=[auth_guard, Permission("manage-users")],
    )
    async def save_user_roles(
        self,
        request: Request,
        db_session: AsyncSession,
        user_id: UUID,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Save user roles."""
        # Get selected roles from form
        selected_roles = set()
        for key in data:
            if key.startswith("role_"):
                role_name = key[5:]  # Remove "role_" prefix
                if data[key] == "on":
                    selected_roles.add(role_name)

        # Get target user's current roles
        result = await db_session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles))
        )
        target_user = result.scalar_one_or_none()
        if not target_user:
            request.session["flash"] = "User not found"
            return Redirect(path="/admin/users")

        current_roles = {role.name for role in target_user.roles}

        # Add new roles
        for role_name in selected_roles - current_roles:
            await assign_role_to_user(db_session, user_id, role_name)

        # Remove unchecked roles
        for role_name in current_roles - selected_roles:
            await remove_role_from_user(db_session, user_id, role_name)

        # Invalidate cache for this user
        invalidate_user_permissions_cache(user_id)

        request.session["flash"] = f"Roles updated for {target_user.name or target_user.email}"
        return Redirect(path="/admin/users")

    @get(
        "/pages",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-pages")],
        opt={"label": "Pages", "icon": "file-text", "order": 20},
    )
    async def list_pages(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List all pages with management actions."""
        ctx = await self._get_admin_context(request, db_session)

        # Get all pages with their authors
        result = await db_session.execute(
            select(Page)
            .options(selectinload(Page.user))
            .order_by(Page.created_at.desc())
        )
        pages = list(result.scalars().all())

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/pages/list.html",
            context={"flash": flash, "pages": pages, **ctx},
        )

    @get(
        "/pages/new",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def new_page(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show new page form."""
        ctx = await self._get_admin_context(request, db_session)
        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/pages/edit.html",
            context={"flash": flash, "page": None, **ctx},
        )

    @post(
        "/pages/new",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def create_page(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Create a new page."""
        title = data.get("title", "").strip()
        slug = data.get("slug", "").strip()
        content = data.get("content", "").strip()
        is_published = data.get("is_published") == "on"

        if not title or not slug:
            request.session["flash"] = "Title and slug are required"
            return Redirect(path="/admin/pages/new")

        published_at = datetime.now(UTC) if is_published else None

        try:
            await page_service.create_page(
                db_session,
                slug=slug,
                title=title,
                content=content,
                is_published=is_published,
                published_at=published_at,
            )
            request.session["flash"] = f"Page '{title}' created successfully!"
            return Redirect(path="/admin/pages")
        except Exception as e:
            request.session["flash"] = f"Error creating page: {str(e)}"
            return Redirect(path="/admin/pages/new")

    @get(
        "/pages/{page_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def edit_page(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> TemplateResponse:
        """Show edit page form."""
        ctx = await self._get_admin_context(request, db_session)

        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            request.session["flash"] = "Page not found"
            return Redirect(path="/admin/pages")

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/pages/edit.html",
            context={"flash": flash, "page": page, **ctx},
        )

    @post(
        "/pages/{page_id:uuid}/edit",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def update_page(
        self,
        request: Request,
        db_session: AsyncSession,
        page_id: UUID,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Update an existing page."""
        title = data.get("title", "").strip()
        slug = data.get("slug", "").strip()
        content = data.get("content", "").strip()
        is_published = data.get("is_published") == "on"

        if not title or not slug:
            request.session["flash"] = "Title and slug are required"
            return Redirect(path=f"/admin/pages/{page_id}/edit")

        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            request.session["flash"] = "Page not found"
            return Redirect(path="/admin/pages")

        published_at = page.published_at
        if is_published and not page.is_published:
            published_at = datetime.now(UTC)

        try:
            await page_service.update_page(
                db_session,
                page_id=page_id,
                slug=slug,
                title=title,
                content=content,
                is_published=is_published,
                published_at=published_at,
            )
            request.session["flash"] = f"Page '{title}' updated successfully!"
            return Redirect(path="/admin/pages")
        except Exception as e:
            request.session["flash"] = f"Error updating page: {str(e)}"
            return Redirect(path=f"/admin/pages/{page_id}/edit")

    @post(
        "/pages/{page_id:uuid}/publish",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def publish_page(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> Redirect:
        """Publish a page."""
        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            request.session["flash"] = "Page not found"
            return Redirect(path="/admin/pages")

        await page_service.update_page(
            db_session,
            page_id=page_id,
            is_published=True,
            published_at=datetime.now(UTC),
        )

        request.session["flash"] = f"'{page.title}' has been published"
        return Redirect(path="/admin/pages")

    @post(
        "/pages/{page_id:uuid}/unpublish",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def unpublish_page(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> Redirect:
        """Unpublish a page."""
        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            request.session["flash"] = "Page not found"
            return Redirect(path="/admin/pages")

        await page_service.update_page(
            db_session,
            page_id=page_id,
            is_published=False,
        )

        request.session["flash"] = f"'{page.title}' has been unpublished"
        return Redirect(path="/admin/pages")

    @post(
        "/pages/{page_id:uuid}/delete",
        guards=[auth_guard, Permission("manage-pages")],
    )
    async def delete_page(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> Redirect:
        """Delete a page."""
        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            request.session["flash"] = "Page not found"
            return Redirect(path="/admin/pages")

        page_title = page.title
        await page_service.delete_page(db_session, page_id)

        request.session["flash"] = f"'{page_title}' has been deleted"
        return Redirect(path="/admin/pages")

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
        ctx = await self._get_admin_context(request, db_session)
        site_settings = await setting_service.get_site_settings(db_session)

        flash = request.session.pop("flash", None)
        return TemplateResponse(
            "admin/settings/site.html",
            context={"flash": flash, "settings": site_settings, **ctx},
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

        # Refresh the site settings cache
        await setting_service.load_site_settings_cache(db_session)

        request.session["flash"] = "Site settings saved successfully"
        return Redirect(path="/admin/settings")
