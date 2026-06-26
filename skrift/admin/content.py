"""Admin controller for editing code-declared content areas."""

from __future__ import annotations

import logging
from typing import Annotated

from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template as TemplateResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.content import (
    build_nodes,
    get_content_area,
    hydrate,
    list_content_areas,
    parse_nested_form,
)
from skrift.db.services import content_service
from skrift.flash import flash_error, flash_success, get_flash_messages

logger = logging.getLogger(__name__)

CONTENT_BASE = "/admin/content"


class ContentAdminController(Controller):
    """List content areas and edit their values."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/content",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("modify-site")],
        opt={"label": "Content", "icon": "layout", "order": 30},
    )
    async def list_content(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        ctx = await get_admin_context(request, db_session)
        areas = [
            {
                "key": key,
                "label": schema._content_label,
                "description": schema._content_description,
            }
            for key, schema in sorted(
                list_content_areas().items(),
                key=lambda item: item[1]._content_label,
            )
        ]
        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/content/list.html",
            context={"flash_messages": flash_messages, "areas": areas, **ctx},
        )

    @get(
        "/content/{key:str}/edit",
        guards=[auth_guard, Permission("modify-site")],
    )
    async def edit_content(
        self, request: Request, db_session: AsyncSession, key: str
    ) -> TemplateResponse | Redirect:
        ctx = await get_admin_context(request, db_session)

        try:
            schema = get_content_area(key)
        except LookupError:
            flash_error(request, f"Unknown content area '{key}'")
            return Redirect(path=CONTENT_BASE)

        saved = await content_service.get_content_data(db_session, key)
        try:
            model = hydrate(schema, saved)
        except ValidationError:
            # Stored data predates a schema change; start from defaults.
            model = schema()
        nodes = build_nodes(schema, model.model_dump())

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "admin/content/edit.html",
            context={
                "flash_messages": flash_messages,
                "content_key": key,
                "content_label": schema._content_label,
                "content_description": schema._content_description,
                "nodes": nodes,
                **ctx,
            },
        )

    @post(
        "/content/{key:str}/edit",
        guards=[auth_guard, Permission("modify-site")],
    )
    async def save_content(
        self,
        request: Request,
        db_session: AsyncSession,
        key: str,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        try:
            schema = get_content_area(key)
        except LookupError:
            flash_error(request, f"Unknown content area '{key}'")
            return Redirect(path=CONTENT_BASE)

        parsed = parse_nested_form(data)
        try:
            model = schema(**parsed)
        except ValidationError as error:
            logger.info("Content validation failed for %s: %s", key, error)
            flash_error(request, "Some fields were invalid. Please review and try again.")
            return Redirect(path=f"{CONTENT_BASE}/{key}/edit")

        await content_service.save_content_data(db_session, key, model.model_dump(mode="json"))
        flash_success(request, f"{schema._content_label} updated successfully")
        return Redirect(path=CONTENT_BASE)
