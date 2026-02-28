"""Media library admin controller."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Response
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.db.services.asset_service import (
    UploadTooLargeError,
    count_assets,
    delete_asset,
    get_asset_url,
    list_assets,
    upload_asset,
)
from skrift.lib.flash import flash_error, flash_success, get_flash_messages
from skrift.lib.storage import StorageManager


class MediaAdminController(Controller):
    """Controller for the admin media library."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/media",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("manage-media")],
        opt={"label": "Media", "icon": "image", "order": 15},
    )
    async def media_library(
        self,
        request: Request,
        db_session: AsyncSession,
        page: int = 1,
        store: str | None = None,
    ) -> TemplateResponse:
        """Browse uploaded media assets."""
        ctx = await get_admin_context(request, db_session)

        per_page = 24
        offset = (page - 1) * per_page

        assets = await list_assets(db_session, store=store, limit=per_page, offset=offset)
        total = await count_assets(db_session, store=store)

        storage: StorageManager = request.app.state.storage_manager
        asset_urls = {}
        for asset in assets:
            backend = await storage.get(asset.store)
            asset_urls[str(asset.id)] = await backend.get_url(asset.key)

        total_pages = max(1, (total + per_page - 1) // per_page)

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/media/library.html",
            context={
                "flash_messages": flash_messages,
                "assets": assets,
                "asset_urls": asset_urls,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "store_names": storage.store_names,
                "current_store": store,
                **ctx,
            },
        )

    @post(
        "/media/upload",
        guards=[auth_guard, Permission("upload-media")],
    )
    async def upload_media(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[UploadFile, Body(media_type=RequestEncodingType.MULTI_PART)],
    ) -> Redirect:
        """Handle file upload from the media library form."""
        storage: StorageManager = request.app.state.storage_manager
        user = getattr(request, "user", None)
        user_id = user.id if user else None

        content = await data.read()
        filename = data.filename or "untitled"
        content_type = data.content_type or "application/octet-stream"

        try:
            await upload_asset(
                db_session,
                storage,
                filename=filename,
                data=content,
                content_type=content_type,
                user_id=user_id,
            )
            flash_success(request, f"Uploaded {filename}")
        except UploadTooLargeError:
            flash_error(request, f"File too large: {filename}")
        except Exception as exc:
            flash_error(request, f"Upload failed: {exc}")

        return Redirect(path="/admin/media")

    @post(
        "/media/upload-json",
        guards=[auth_guard, Permission("upload-media")],
    )
    async def upload_media_json(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[UploadFile, Body(media_type=RequestEncodingType.MULTI_PART)],
    ) -> Response:
        """Handle file upload and return JSON (used by page editor fetch)."""
        storage: StorageManager = request.app.state.storage_manager
        uid = request.session.get(SESSION_USER_ID)
        user_id = UUID(uid) if uid else None

        content = await data.read()
        filename = data.filename or "untitled"
        content_type = data.content_type or "application/octet-stream"

        try:
            asset = await upload_asset(
                db_session,
                storage,
                filename=filename,
                data=content,
                content_type=content_type,
                user_id=user_id,
            )
        except UploadTooLargeError:
            return Response(
                content={"error": f"File too large: {filename}"},
                status_code=413,
                media_type="application/json",
            )
        except Exception as exc:
            return Response(
                content={"error": f"Upload failed: {exc}"},
                status_code=500,
                media_type="application/json",
            )

        url = await get_asset_url(storage, asset)
        return Response(
            content={
                "id": str(asset.id),
                "filename": asset.filename,
                "url": url,
                "content_type": asset.content_type,
                "size": asset.size,
            },
            status_code=201,
            media_type="application/json",
        )

    @post(
        "/media/{asset_id:uuid}/delete",
        guards=[auth_guard, Permission("manage-media")],
    )
    async def delete_media(
        self,
        request: Request,
        db_session: AsyncSession,
        asset_id: UUID,
    ) -> Redirect:
        """Delete an asset."""
        storage: StorageManager = request.app.state.storage_manager
        deleted = await delete_asset(db_session, storage, asset_id)

        if deleted:
            flash_success(request, "Asset deleted.")
        else:
            flash_error(request, "Asset not found.")

        return Redirect(path="/admin/media")

    @get(
        "/media/picker",
        guards=[auth_guard, Permission("upload-media")],
    )
    async def media_picker(
        self,
        request: Request,
        db_session: AsyncSession,
        page: int = 1,
    ) -> TemplateResponse:
        """Embeddable asset picker fragment for editors."""
        per_page = 20
        offset = (page - 1) * per_page
        assets = await list_assets(db_session, limit=per_page, offset=offset)
        total = await count_assets(db_session)

        storage: StorageManager = request.app.state.storage_manager
        asset_urls = {}
        for asset in assets:
            backend = await storage.get(asset.store)
            asset_urls[str(asset.id)] = await backend.get_url(asset.key)

        total_pages = max(1, (total + per_page - 1) // per_page)

        return TemplateResponse(
            "admin/media/picker.html",
            context={
                "assets": assets,
                "asset_urls": asset_urls,
                "page": page,
                "total_pages": total_pages,
            },
        )
