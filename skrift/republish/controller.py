"""HTTP API for optional cross-site republishing."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from litestar import Controller, Request, delete, get, post, put
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException
from litestar.params import Body
from litestar.response import Redirect, Response
from litestar.response import Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.config import get_settings, load_page_types_from_yaml
from skrift.controllers.helpers import get_user_context
from skrift.db.models import APIKey
from skrift.db.services import api_key_service
from skrift.forms import verify_csrf
from skrift.lib.client_ip import get_client_ip
from skrift.flash import flash_error, flash_success, get_flash_messages
from skrift.republish import REPUBLISH_PERMISSION
from skrift.republish.service import (
    apply_republish_delete_behavior,
    get_republish_by_canonical_url,
    origin_from_url,
    parse_datetime,
    save_outbound_link,
    upsert_republished_page,
)


def _json_error(error: str, description: str, status_code: int = 400) -> Response:
    return Response(
        content={"error": error, "error_description": description},
        status_code=status_code,
        media_type="application/json",
    )


def _extract_bearer(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


def _supported_page_types() -> list[str]:
    settings = get_settings()
    configured = list(settings.republish.page_types)
    if not configured:
        configured = [pt.name for pt in load_page_types_from_yaml()]
    if settings.republish.default_page_type not in configured:
        configured.append(settings.republish.default_page_type)
    return sorted(set(configured))


async def _verify_republish_key(
    request: Request,
    db_session: AsyncSession,
) -> tuple[APIKey, dict[str, Any]] | Response:
    bearer = _extract_bearer(request)
    if not bearer or not bearer.startswith("sk_"):
        return _json_error("invalid_client", "Bearer API key required", status_code=401)

    api_key = await api_key_service.verify_api_key(
        db_session,
        bearer,
        client_ip=get_client_ip(request.scope),
    )
    if api_key is None:
        return _json_error("invalid_client", "Invalid API key", status_code=401)
    if REPUBLISH_PERMISSION not in api_key.scoped_permission_list:
        return _json_error("invalid_scope", "API key is not scoped for republishing", status_code=403)

    constraints = api_key.constraint_data.get("republish")
    if not isinstance(constraints, dict):
        return _json_error("invalid_scope", "API key is missing republish constraints", status_code=403)
    if not constraints.get("source_origin"):
        return _json_error("invalid_scope", "API key is missing source-origin constraint", status_code=403)
    if constraints.get("page_type") not in _supported_page_types():
        return _json_error("invalid_scope", "API key has an unsupported page-type constraint", status_code=403)
    if constraints.get("post_behavior") not in {"draft", "publish"}:
        return _json_error("invalid_scope", "API key has an unsupported post-behavior constraint", status_code=403)
    if constraints.get("delete_behavior") not in {"unpublish", "delete", "ignore"}:
        return _json_error("invalid_scope", "API key has an unsupported delete-behavior constraint", status_code=403)
    return api_key, constraints


def _validate_canonical_url(body: dict[str, Any], constraints: dict[str, Any]) -> str | Response:
    canonical_url = str(body.get("canonical_url", "")).strip()
    source_origin = str(constraints.get("source_origin", "")).strip()
    if not canonical_url:
        return _json_error("invalid_request", "canonical_url is required")
    if origin_from_url(canonical_url) != source_origin:
        return _json_error("invalid_request", "canonical_url origin does not match this API key", status_code=403)
    return canonical_url


class RepublishController(Controller):
    """Cross-site republishing endpoints."""

    path = "/api/republish"

    @get("/capabilities")
    async def capabilities(self, request: Request) -> Response:
        settings = get_settings()
        if not settings.republish.enabled:
            raise NotFoundException()

        issuer = str(request.base_url).rstrip("/")
        return Response(
            content={
                "republish": True,
                "version": 1,
                "schema": "baseline-v1",
                "permission": REPUBLISH_PERMISSION,
                "endpoints": {
                    "upsert": f"{issuer}/api/republish/posts",
                    "delete": f"{issuer}/api/republish/posts",
                },
                "page_types": _supported_page_types(),
                "post_behaviors": ["draft", "publish"],
                "delete_behaviors": ["unpublish", "delete", "ignore"],
                "media": {"mode": "hotlink"},
            },
            status_code=200,
            media_type="application/json",
        )

    @put("/posts")
    async def upsert_post(self, request: Request, db_session: AsyncSession) -> Response:
        settings = get_settings()
        if not settings.republish.enabled:
            raise NotFoundException()

        verified = await _verify_republish_key(request, db_session)
        if isinstance(verified, Response):
            return verified
        api_key, constraints = verified

        body = await request.json()
        if not isinstance(body, dict):
            return _json_error("invalid_request", "JSON object body is required")

        canonical_url = _validate_canonical_url(body, constraints)
        if isinstance(canonical_url, Response):
            return canonical_url

        title = str(body.get("title", "")).strip()
        content = str(body.get("content", "")).strip()
        if not title or not content:
            return _json_error("invalid_request", "title and content are required")

        tags = body.get("tags")
        if tags is not None and not isinstance(tags, list):
            return _json_error("invalid_request", "tags must be a list")

        metadata, created = await upsert_republished_page(
            db_session,
            user_id=UUID(str(api_key.user_id)),
            canonical_url=canonical_url,
            source_origin=str(constraints["source_origin"]),
            page_type=str(constraints["page_type"]),
            post_behavior=str(constraints["post_behavior"]),
            title=title,
            content=content,
            summary=str(body.get("summary", "")).strip(),
            author_name=str(body.get("author_name", "")).strip(),
            image_url=str(body.get("image_url", "")).strip(),
            tags=[str(tag) for tag in tags] if tags else [],
            source_updated_at=parse_datetime(body.get("updated_at")),
        )

        return Response(
            content={
                "status": "created" if created else "updated",
                "page_id": str(metadata.page_id),
                "canonical_url": metadata.canonical_url,
                "source_origin": metadata.source_origin,
            },
            status_code=201 if created else 200,
            media_type="application/json",
        )

    @delete("/posts")
    async def delete_post(self, request: Request, db_session: AsyncSession) -> Response:
        settings = get_settings()
        if not settings.republish.enabled:
            raise NotFoundException()

        verified = await _verify_republish_key(request, db_session)
        if isinstance(verified, Response):
            return verified
        _, constraints = verified

        body = await request.json()
        if not isinstance(body, dict):
            return _json_error("invalid_request", "JSON object body is required")

        canonical_url = _validate_canonical_url(body, constraints)
        if isinstance(canonical_url, Response):
            return canonical_url

        metadata = await get_republish_by_canonical_url(db_session, canonical_url)
        if metadata is None:
            return _json_error("not_found", "Republished page not found", status_code=404)

        status = await apply_republish_delete_behavior(
            db_session,
            metadata,
            behavior=str(constraints["delete_behavior"]),
        )
        return Response(
            content={"status": status, "canonical_url": canonical_url},
            status_code=200,
            media_type="application/json",
        )


class AccountRepublishController(Controller):
    """Account routes for managing republish links."""

    path = "/account/republish"
    guards = [auth_guard]

    @get("/links/new")
    async def new_link(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        settings = get_settings()
        if not settings.republish.enabled:
            raise NotFoundException()
        user_ctx = await get_user_context(request, db_session)
        return TemplateResponse(
            "republish/link_site.html",
            context={
                "request": request,
                "flash_messages": get_flash_messages(request),
                **user_ctx,
            },
        )

    @post("/links/new")
    async def create_link(
        self,
        request: Request,
        db_session: AsyncSession,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        settings = get_settings()
        if not settings.republish.enabled:
            raise NotFoundException()
        if not await verify_csrf(request):
            flash_error(request, "Invalid CSRF token")
            return Redirect(path="/account/republish/links/new")

        site_url = str(data.get("site_url", "")).strip()
        site_name = str(data.get("site_name", "")).strip()
        if not origin_from_url(site_url):
            flash_error(request, "Enter an absolute HTTP(S) site URL.")
            return Redirect(path="/account/republish/links/new")

        user_id = request.session.get(SESSION_USER_ID)
        await save_outbound_link(
            db_session,
            user_id=str(user_id),
            site_url=site_url,
            site_name=site_name,
        )
        flash_success(request, "Site linked for republishing.")
        return Redirect(path="/account")
