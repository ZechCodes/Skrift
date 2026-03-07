"""Dynamic controller factory for per-type page admin sections."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import (
    check_page_access,
    extract_page_form_data,
    get_admin_context,
)
from skrift.admin.page_operations import (
    create_typed_page,
    list_pages_for_admin,
    update_typed_page,
)
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import OwnerOrPermission, Permission, auth_guard
from skrift.auth.roles import permissions_for_type
from skrift.config import PageTypeConfig
from skrift.db.services import page_service, revision_service
from skrift.db.services.asset_service import get_asset_url
from skrift.lib.flash import flash_error, flash_success, get_flash_messages
from skrift.lib.storage import StorageManager

logger = logging.getLogger(__name__)


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

    page_type_ctx = {
        "page_type_name": type_name,
        "page_type_plural": plural,
        "page_type_label": label,
        "page_type_label_plural": label_plural,
        "admin_type_base": admin_base,
    }

    def _get_page_or_redirect(page, request):
        if page is None:
            flash_error(request, f"{label} not found")
            return Redirect(path=admin_base)
        return None

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
            pages = await list_pages_for_admin(
                db_session,
                page_type_name=type_name,
                user_id=UUID(request.session[SESSION_USER_ID]),
                permissions=ctx["permissions"],
                manage_permission=perms["manage"],
            )

            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/pages/list.html",
                context={
                    "flash_messages": flash_messages,
                    "pages": pages,
                    **page_type_ctx,
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
                    "page_assets": [],
                    "asset_urls": {},
                    **page_type_ctx,
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

            user_id = UUID(request.session[SESSION_USER_ID])

            try:
                page = await create_typed_page(
                    db_session,
                    user_id=user_id,
                    form=form,
                    page_type_name=type_name,
                )
                flash_success(request, f"{label} '{form.title}' created successfully!")
                return Redirect(path=admin_base)
            except Exception:
                logger.exception("Admin %s create failed", type_name)
                flash_error(
                    request,
                    f"Could not create {label.lower()}. Check the server logs and try again.",
                )
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
            if redirect := _get_page_or_redirect(page, request):
                return redirect

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

            # Resolve asset URLs for attached assets
            storage: StorageManager = request.app.state.storage_manager
            asset_urls = {
                str(asset.id): await get_asset_url(storage, asset)
                for asset in page.assets
            }

            flash_messages = get_flash_messages(request)
            return TemplateResponse(
                "admin/pages/edit.html",
                context={
                    "flash_messages": flash_messages,
                    "page": page,
                    "page_assets": page.assets,
                    "asset_urls": asset_urls,
                    **page_type_ctx,
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
            if redirect := _get_page_or_redirect(page, request):
                return redirect

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

            try:
                await update_typed_page(
                    db_session,
                    page=page,
                    form=form,
                    user_id=UUID(request.session[SESSION_USER_ID]),
                    page_type_name=type_name,
                )
                flash_success(request, f"{label} '{form.title}' updated successfully!")
                return Redirect(path=admin_base)
            except Exception:
                logger.exception("Admin %s update failed", type_name)
                flash_error(
                    request,
                    f"Could not update {label.lower()}. Check the server logs and try again.",
                )
                return Redirect(path=f"{admin_base}/{page_id}/edit")

        @post(
            f"/{plural}/{{page_id:uuid}}/publish",
            guards=[auth_guard, Permission(perms["manage"])],
        )
        async def publish_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if redirect := _get_page_or_redirect(page, request):
                return redirect

            await page_service.update_page(
                db_session,
                page_id=page_id,
                is_published=True,
                published_at=datetime.now(UTC),
            )

            flash_success(request, f"'{page.title}' has been published")
            return Redirect(path=admin_base)

        @post(
            f"/{plural}/{{page_id:uuid}}/unpublish",
            guards=[auth_guard, Permission(perms["manage"])],
        )
        async def unpublish_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if redirect := _get_page_or_redirect(page, request):
                return redirect

            await page_service.update_page(
                db_session,
                page_id=page_id,
                is_published=False,
            )

            flash_success(request, f"'{page.title}' has been unpublished")
            return Redirect(path=admin_base)

        @post(
            f"/{plural}/{{page_id:uuid}}/delete",
            guards=[auth_guard, OwnerOrPermission(perms["delete_own"], perms["manage"])],
        )
        async def delete_page(
            self, request: Request, db_session: AsyncSession, page_id: UUID
        ) -> Redirect:
            page = await page_service.get_page_by_id(db_session, page_id)
            if redirect := _get_page_or_redirect(page, request):
                return redirect

            await check_page_access(
                db_session, request, page, perms["delete_own"], perms["manage"]
            )

            page_title = page.title
            await page_service.delete_page(db_session, page_id)

            flash_success(request, f"'{page_title}' has been deleted")
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
            if redirect := _get_page_or_redirect(page, request):
                return redirect

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
                    **page_type_ctx,
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
            if redirect := _get_page_or_redirect(page, request):
                return redirect

            await check_page_access(
                db_session, request, page, perms["edit_own"], perms["manage"]
            )

            revision = await revision_service.get_revision(db_session, revision_id)
            if not revision or revision.page_id != page_id:
                flash_error(request, "Revision not found")
                return Redirect(path=f"{admin_base}/{page_id}/revisions")

            user_id = request.session.get(SESSION_USER_ID)
            await revision_service.restore_revision(
                db_session, page, revision, UUID(user_id) if user_id else None
            )

            flash_success(request, f"{label} restored to revision #{revision.revision_number}")
            return Redirect(path=f"{admin_base}/{page_id}/edit")

    # Give the class a unique name for Litestar's route registration
    _PageTypeController.__name__ = f"{label}AdminController"
    _PageTypeController.__qualname__ = f"{label}AdminController"

    return _PageTypeController
