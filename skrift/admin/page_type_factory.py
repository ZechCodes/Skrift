"""Dynamic controller factory for per-type page admin sections."""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.admin.helpers import (
    check_page_access,
    extract_page_form_data,
    get_admin_context,
)
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import OwnerOrPermission, Permission, auth_guard
from skrift.auth.roles import permissions_for_type
from skrift.config import PageTypeConfig
from skrift.db.models import Page
from skrift.db.services import page_service, revision_service
from skrift.lib.flash import flash_error, flash_success, get_flash_messages


def create_page_type_controller(page_type: PageTypeConfig) -> type[Controller]:
    """Create a Controller subclass for a specific page type.

    Each generated controller:
    - Routes under /admin/{plural}
    - Filters all queries by Page.type == type_name
    - Uses type-specific permissions
    - Has its own admin nav entry
    """
    type_name = page_type.name          # "post"
    plural = page_type.plural            # "posts"
    icon = page_type.icon                # "pen-tool"
    nav_order = page_type.nav_order      # 10
    label = type_name.title()            # "Post"
    label_plural = plural.title()        # "Posts"
    admin_base = f"/admin/{plural}"      # "/admin/posts"
    perms = permissions_for_type(plural)  # {"manage": "manage-posts", ...}

    class _PageTypeController(Controller):
        path = "/admin"
        guards = [auth_guard]

        @get(
            f"/{plural}",
            tags=[ADMIN_NAV_TAG],
            guards=[auth_guard, OwnerOrPermission(perms["edit_own"], perms["manage"])],
            opt={"label": label_plural, "icon": icon, "order": nav_order},
        )
        async def list_pages(
            self, request: Request, db_session: AsyncSession
        ) -> TemplateResponse:
            ctx = await get_admin_context(request, db_session)
            permissions = ctx["permissions"]

            query = (
                select(Page)
                .where(Page.type == type_name)
                .options(selectinload(Page.user))
                .order_by(Page.order.asc(), Page.created_at.desc())
            )

            if (
                "administrator" not in permissions.permissions
                and perms["manage"] not in permissions.permissions
            ):
                user_id = UUID(request.session["user_id"])
                query = query.where(Page.user_id == user_id)

            result = await db_session.execute(query)
            pages = list(result.scalars().all())

            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/pages/list.html",
                context={
                    "flash_messages": flash_messages,
                    "pages": pages,
                    "page_type_name": type_name,
                    "page_type_plural": plural,
                    "page_type_label": label,
                    "page_type_label_plural": label_plural,
                    "admin_type_base": admin_base,
                    **ctx,
                },
            )

        @get(
            f"/{plural}/new",
            guards=[auth_guard, OwnerOrPermission(perms["create"], perms["manage"])],
        )
        async def new_page(
            self, request: Request, db_session: AsyncSession
        ) -> TemplateResponse:
            ctx = await get_admin_context(request, db_session)
            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/pages/edit.html",
                context={
                    "flash_messages": flash_messages,
                    "page": None,
                    "page_type_name": type_name,
                    "page_type_plural": plural,
                    "page_type_label": label,
                    "page_type_label_plural": label_plural,
                    "admin_type_base": admin_base,
                    **ctx,
                },
            )

        @post(
            f"/{plural}/new",
            guards=[auth_guard, OwnerOrPermission(perms["create"], perms["manage"])],
        )
        async def create_page(
            self,
            request: Request,
            db_session: AsyncSession,
            data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
        ) -> Redirect:
            try:
                form = extract_page_form_data(data)
            except ValueError as e:
                flash_error(request, str(e))
                return Redirect(path=f"{admin_base}/new")

            if not form.title or not form.slug:
                flash_error(request, "Title and slug are required")
                return Redirect(path=f"{admin_base}/new")

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
                    page_type=type_name,
                )
                flash_success(request, f"{label} '{form.title}' created successfully!")
                return Redirect(path=admin_base)
            except Exception as e:
                flash_error(request, f"Error creating {type_name}: {str(e)}")
                return Redirect(path=f"{admin_base}/new")

        @get(
            f"/{plural}/{{page_id:uuid}}/edit",
            guards=[auth_guard, OwnerOrPermission(perms["edit_own"], perms["manage"])],
        )
        async def edit_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> TemplateResponse:
            ctx = await get_admin_context(request, db_session)

            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                flash_error(request, f"{label} not found")
                return Redirect(path=admin_base)

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/pages/edit.html",
                context={
                    "flash_messages": flash_messages,
                    "page": page,
                    "page_type_name": type_name,
                    "page_type_plural": plural,
                    "page_type_label": label,
                    "page_type_label_plural": label_plural,
                    "admin_type_base": admin_base,
                    **ctx,
                },
            )

        @post(
            f"/{plural}/{{page_id:uuid}}/edit",
            guards=[auth_guard, OwnerOrPermission(perms["edit_own"], perms["manage"])],
        )
        async def update_page(
            self,
            request: Request,
            db_session: AsyncSession,
            page_id: UUID,
            data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
        ) -> Redirect:
            try:
                form = extract_page_form_data(data)
            except ValueError as e:
                flash_error(request, str(e))
                return Redirect(path=f"{admin_base}/{page_id}/edit")

            if not form.title or not form.slug:
                flash_error(request, "Title and slug are required")
                return Redirect(path=f"{admin_base}/{page_id}/edit")

            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                flash_error(request, f"{label} not found")
                return Redirect(path=admin_base)

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

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
                    page_type=type_name,
                )
                flash_success(request, f"{label} '{form.title}' updated successfully!")
                return Redirect(path=admin_base)
            except Exception as e:
                flash_error(request, f"Error updating {type_name}: {str(e)}")
                return Redirect(path=f"{admin_base}/{page_id}/edit")

        @post(
            f"/{plural}/{{page_id:uuid}}/publish",
            guards=[auth_guard, Permission(perms["manage"])],
        )
        async def publish_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                request.session["flash"] = f"{label} not found"
                return Redirect(path=admin_base)

            await page_service.update_page(
                db_session,
                page_id=page_id,
                is_published=True,
                published_at=datetime.now(UTC),
            )

            request.session["flash"] = f"'{page.title}' has been published"
            return Redirect(path=admin_base)

        @post(
            f"/{plural}/{{page_id:uuid}}/unpublish",
            guards=[auth_guard, Permission(perms["manage"])],
        )
        async def unpublish_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                request.session["flash"] = f"{label} not found"
                return Redirect(path=admin_base)

            await page_service.update_page(
                db_session,
                page_id=page_id,
                is_published=False,
            )

            request.session["flash"] = f"'{page.title}' has been unpublished"
            return Redirect(path=admin_base)

        @post(
            f"/{plural}/{{page_id:uuid}}/delete",
            guards=[auth_guard, OwnerOrPermission(perms["delete_own"], perms["manage"])],
        )
        async def delete_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                request.session["flash"] = f"{label} not found"
                return Redirect(path=admin_base)

            await check_page_access(
                db_session, request, page, perms["delete_own"], perms["manage"]
            )

            page_title = page.title
            await page_service.delete_page(db_session, page_id)

            request.session["flash"] = f"'{page_title}' has been deleted"
            return Redirect(path=admin_base)

        @get(
            f"/{plural}/{{page_id:uuid}}/revisions",
            guards=[auth_guard, OwnerOrPermission(perms["edit_own"], perms["manage"])],
        )
        async def list_revisions(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> TemplateResponse:
            ctx = await get_admin_context(request, db_session)

            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                flash_error(request, f"{label} not found")
                return Redirect(path=admin_base)

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

            revisions = await revision_service.list_revisions(db_session, page_id)

            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/pages/revisions.html",
                context={
                    "flash_messages": flash_messages,
                    "page": page,
                    "revisions": revisions,
                    "page_type_name": type_name,
                    "page_type_plural": plural,
                    "page_type_label": label,
                    "page_type_label_plural": label_plural,
                    "admin_type_base": admin_base,
                    **ctx,
                },
            )

        @post(
            f"/{plural}/{{page_id:uuid}}/revisions/{{revision_id:uuid}}/restore",
            guards=[auth_guard, OwnerOrPermission(perms["edit_own"], perms["manage"])],
        )
        async def restore_revision(
            self,
            request: Request,
            db_session: AsyncSession,
            page_id: UUID,
            revision_id: UUID,
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if not page:
                flash_error(request, f"{label} not found")
                return Redirect(path=admin_base)

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

            revision = await revision_service.get_revision(db_session, revision_id)
            if not revision or revision.page_id != page_id:
                flash_error(request, "Revision not found")
                return Redirect(path=f"{admin_base}/{page_id}/revisions")

            user_id = request.session.get("user_id")
            await revision_service.restore_revision(
                db_session, page, revision, UUID(user_id) if user_id else None
            )

            flash_success(request, f"{label} restored to revision #{revision.revision_number}")
            return Redirect(path=f"{admin_base}/{page_id}/edit")

    # Give the class a unique name for Litestar's route registration
    _PageTypeController.__name__ = f"{label}AdminController"
    _PageTypeController.__qualname__ = f"{label}AdminController"

    return _PageTypeController
