"""Page management admin controller."""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse, Redirect
from litestar.params import Body
from litestar.enums import RequestEncodingType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.auth.guards import auth_guard, Permission, OwnerOrPermission
from skrift.admin.helpers import (
    extract_page_form_data,
    get_admin_context,
    check_page_access,
)
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.db.models import Page
from skrift.db.services import page_service, revision_service
from skrift.lib.flash import flash_success, flash_error, get_flash_messages


class PageAdminController(Controller):
    """Controller for page management in admin."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/pages",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, OwnerOrPermission("edit-own-pages", "manage-pages")],
        opt={"label": "Pages", "icon": "file-text", "order": 20},
    )
    async def list_pages(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List pages — editors see all, authors see only their own."""
        ctx = await get_admin_context(request, db_session)
        permissions = ctx["permissions"]

        if "administrator" in permissions.permissions or "manage-pages" in permissions.permissions:
            # Editors/admins see all pages
            result = await db_session.execute(
                select(Page)
                .options(selectinload(Page.user))
                .order_by(Page.order.asc(), Page.created_at.desc())
            )
            pages = list(result.scalars().all())
        else:
            # Authors see only their own pages
            user_id = UUID(request.session["user_id"])
            result = await db_session.execute(
                select(Page)
                .options(selectinload(Page.user))
                .where(Page.user_id == user_id)
                .order_by(Page.order.asc(), Page.created_at.desc())
            )
            pages = list(result.scalars().all())

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/pages/list.html",
            context={"flash_messages": flash_messages, "pages": pages, **ctx},
        )

    @get(
        "/pages/new",
        guards=[auth_guard, OwnerOrPermission("create-pages", "manage-pages")],
    )
    async def new_page(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show new page form."""
        ctx = await get_admin_context(request, db_session)
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/pages/edit.html",
            context={"flash_messages": flash_messages, "page": None, **ctx},
        )

    @post(
        "/pages/new",
        guards=[auth_guard, OwnerOrPermission("create-pages", "manage-pages")],
    )
    async def create_page(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Create a new page."""
        try:
            form = extract_page_form_data(data)
        except ValueError as e:
            flash_error(request, str(e))
            return Redirect(path="/admin/pages/new")

        if not form.title or not form.slug:
            flash_error(request, "Title and slug are required")
            return Redirect(path="/admin/pages/new")

        published_at = datetime.now(UTC) if form.is_published else None
        user_id = UUID(request.session["user_id"])

        try:
            await page_service.create_page(
                db_session,
                slug=form.slug,
                title=form.title,
                content=form.content,
                is_published=form.is_published,
                published_at=published_at,
                order=form.order,
                publish_at=form.publish_at,
                meta_description=form.meta_description,
                og_title=form.og_title,
                og_description=form.og_description,
                og_image=form.og_image,
                meta_robots=form.meta_robots,
                user_id=user_id,
            )
            flash_success(request, f"Page '{form.title}' created successfully!")
            return Redirect(path="/admin/pages")
        except Exception as e:
            flash_error(request, f"Error creating page: {str(e)}")
            return Redirect(path="/admin/pages/new")

    @get(
        "/pages/{page_id:uuid}/edit",
        guards=[auth_guard, OwnerOrPermission("edit-own-pages", "manage-pages")],
    )
    async def edit_page(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> TemplateResponse:
        """Show edit page form."""
        ctx = await get_admin_context(request, db_session)

        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            flash_error(request, "Page not found")
            return Redirect(path="/admin/pages")

        await check_page_access(db_session, request, page, "edit-own-pages", "manage-pages")

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/pages/edit.html",
            context={"flash_messages": flash_messages, "page": page, **ctx},
        )

    @post(
        "/pages/{page_id:uuid}/edit",
        guards=[auth_guard, OwnerOrPermission("edit-own-pages", "manage-pages")],
    )
    async def update_page(
        self,
        request: Request,
        db_session: AsyncSession,
        page_id: UUID,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        """Update an existing page."""
        try:
            form = extract_page_form_data(data)
        except ValueError as e:
            flash_error(request, str(e))
            return Redirect(path=f"/admin/pages/{page_id}/edit")

        if not form.title or not form.slug:
            flash_error(request, "Title and slug are required")
            return Redirect(path=f"/admin/pages/{page_id}/edit")

        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            flash_error(request, "Page not found")
            return Redirect(path="/admin/pages")

        await check_page_access(db_session, request, page, "edit-own-pages", "manage-pages")

        published_at = page.published_at
        if form.is_published and not page.is_published:
            published_at = datetime.now(UTC)

        try:
            await page_service.update_page(
                db_session,
                page_id=page_id,
                slug=form.slug,
                title=form.title,
                content=form.content,
                is_published=form.is_published,
                published_at=published_at,
                order=form.order,
                publish_at=form.publish_at,
                meta_description=form.meta_description,
                og_title=form.og_title,
                og_description=form.og_description,
                og_image=form.og_image,
                meta_robots=form.meta_robots,
            )
            flash_success(request, f"Page '{form.title}' updated successfully!")
            return Redirect(path="/admin/pages")
        except Exception as e:
            flash_error(request, f"Error updating page: {str(e)}")
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
        guards=[auth_guard, OwnerOrPermission("delete-own-pages", "manage-pages")],
    )
    async def delete_page(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> Redirect:
        """Delete a page."""
        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            request.session["flash"] = "Page not found"
            return Redirect(path="/admin/pages")

        await check_page_access(db_session, request, page, "delete-own-pages", "manage-pages")

        page_title = page.title
        await page_service.delete_page(db_session, page_id)

        request.session["flash"] = f"'{page_title}' has been deleted"
        return Redirect(path="/admin/pages")

    @get(
        "/pages/{page_id:uuid}/revisions",
        guards=[auth_guard, OwnerOrPermission("edit-own-pages", "manage-pages")],
    )
    async def list_revisions(
        self, request: Request, db_session: AsyncSession, page_id: UUID
    ) -> TemplateResponse:
        """List revisions for a page."""
        ctx = await get_admin_context(request, db_session)

        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            flash_error(request, "Page not found")
            return Redirect(path="/admin/pages")

        await check_page_access(db_session, request, page, "edit-own-pages", "manage-pages")

        revisions = await revision_service.list_revisions(db_session, page_id)

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/pages/revisions.html",
            context={"flash_messages": flash_messages, "page": page, "revisions": revisions, **ctx},
        )

    @post(
        "/pages/{page_id:uuid}/revisions/{revision_id:uuid}/restore",
        guards=[auth_guard, OwnerOrPermission("edit-own-pages", "manage-pages")],
    )
    async def restore_revision(
        self, request: Request, db_session: AsyncSession, page_id: UUID, revision_id: UUID
    ) -> Redirect:
        """Restore a page to a previous revision."""
        page = await page_service.get_page_by_id(db_session, page_id)
        if not page:
            flash_error(request, "Page not found")
            return Redirect(path="/admin/pages")

        await check_page_access(db_session, request, page, "edit-own-pages", "manage-pages")

        revision = await revision_service.get_revision(db_session, revision_id)
        if not revision or revision.page_id != page_id:
            flash_error(request, "Revision not found")
            return Redirect(path=f"/admin/pages/{page_id}/revisions")

        user_id = request.session.get("user_id")
        await revision_service.restore_revision(
            db_session, page, revision, UUID(user_id) if user_id else None
        )

        flash_success(request, f"Page restored to revision #{revision.revision_number}")
        return Redirect(path=f"/admin/pages/{page_id}/edit")
