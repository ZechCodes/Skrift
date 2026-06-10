"""Hook handlers for the optional republish component."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from litestar.response import Response
from sqlalchemy import select

from skrift.config import get_settings, load_page_types_from_yaml
from skrift.db.models import PageRepublish
from skrift.lib.hooks import (
    API_GRANT_AUTHORIZE_CONSTRAINTS,
    API_GRANT_AUTHORIZE_CONTEXT,
    API_GRANT_AUTHORIZE_FRAGMENTS,
    ACCOUNT_PAGE_CONTEXT,
    ACCOUNT_PAGE_SECTIONS,
    PAGE_ADMIN_CAN_MUTATE,
    PAGE_ADMIN_PAGE_STATE,
    hooks,
)
from skrift.republish import REPUBLISH_PERMISSION
from skrift.republish.service import list_inbound_publishers, list_outbound_links


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _json_error(error: str, description: str, status_code: int = 400) -> Response:
    return Response(
        content={"error": error, "error_description": description},
        status_code=status_code,
        media_type="application/json",
    )


def _supported_page_types() -> list[str]:
    settings = get_settings()
    configured = list(settings.republish.page_types)
    if not configured:
        configured = [pt.name for pt in load_page_types_from_yaml()]
    if settings.republish.default_page_type not in configured:
        configured.append(settings.republish.default_page_type)
    return sorted(set(configured))


def _is_republish_grant(grant_data: dict[str, Any]) -> bool:
    return REPUBLISH_PERMISSION in set(grant_data.get("permissions", []))


async def add_grant_context(
    context: dict[str, Any],
    request,
    grant_data: dict[str, Any],
) -> dict[str, Any]:
    if not _is_republish_grant(grant_data):
        return context
    settings = get_settings()
    context["republish_options"] = {
        "page_types": _supported_page_types(),
        "default_page_type": settings.republish.default_page_type,
        "post_behaviors": ["draft", "publish"],
        "default_post_behavior": settings.republish.default_post_behavior,
        "delete_behaviors": ["unpublish", "delete", "ignore"],
        "default_delete_behavior": settings.republish.default_delete_behavior,
    }
    return context


async def add_grant_fragment(
    fragments: list[dict[str, Any]],
    request,
    grant_data: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if _is_republish_grant(grant_data):
        fragments.append(
            {
                "slot": "after_permissions",
                "template": "republish/grant_options.html",
            }
        )
    return fragments


async def add_grant_constraints(
    constraints: dict[str, Any],
    request,
    grant_data: dict[str, Any],
    form_data,
) -> dict[str, Any] | Response:
    if not _is_republish_grant(grant_data):
        return constraints

    source_origin = _origin_from_url(grant_data.get("service_url", "")) or _origin_from_url(
        grant_data.get("redirect_uri", "")
    )
    if not source_origin:
        return _json_error("invalid_request", "Republish grants require a valid source origin")

    page_type = str(form_data.get("republish_page_type", "")).strip()
    post_behavior = str(form_data.get("republish_post_behavior", "")).strip()
    delete_behavior = str(form_data.get("republish_delete_behavior", "")).strip()

    settings = get_settings()
    page_type = page_type or settings.republish.default_page_type
    post_behavior = post_behavior or settings.republish.default_post_behavior
    delete_behavior = delete_behavior or settings.republish.default_delete_behavior

    if page_type not in _supported_page_types():
        return _json_error("invalid_request", "Unsupported republish page type")
    if post_behavior not in {"draft", "publish"}:
        return _json_error("invalid_request", "Unsupported republish post behavior")
    if delete_behavior not in {"unpublish", "delete", "ignore"}:
        return _json_error("invalid_request", "Unsupported republish delete behavior")

    constraints["republish"] = {
        "schema": "baseline-v1",
        "source_origin": source_origin,
        "page_type": page_type,
        "post_behavior": post_behavior,
        "delete_behavior": delete_behavior,
    }
    return constraints


async def mark_republish_admin_state(
    state: dict[str, Any],
    request,
    db_session,
    page,
) -> dict[str, Any]:
    republish = await _get_page_republish(db_session, page)
    if republish is None:
        return state
    state.update(
        {
            "locked": True,
            "source_origin": republish.source_origin,
            "badges": [*state.get("badges", []), "Repost"],
        }
    )
    return state


async def prevent_republish_admin_mutation(
    allowed: bool,
    request,
    db_session,
    page,
    action: str,
) -> bool:
    if await _get_page_republish(db_session, page) is not None:
        return False
    return allowed


async def _get_page_republish(db_session, page) -> PageRepublish | None:
    loaded = getattr(page, "__dict__", {}).get("republish")
    if loaded is not None:
        return loaded
    result = await db_session.execute(
        select(PageRepublish).where(PageRepublish.page_id == page.id)
    )
    return result.scalar_one_or_none()


async def add_account_context(
    context: dict[str, Any],
    request,
    db_session,
    user,
) -> dict[str, Any]:
    context["republish_account"] = {
        "outbound_links": await list_outbound_links(db_session, user_id=str(user.id)),
        "inbound_publishers": await list_inbound_publishers(db_session, user_id=str(user.id)),
    }
    return context


async def add_account_section(
    sections: list[dict[str, Any]],
    request,
    db_session,
    user,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    sections.append({"template": "republish/account_section.html"})
    return sections


_configured = False


def setup_republish_hooks() -> None:
    """Register republish hook handlers once."""
    global _configured
    if _configured:
        return
    hooks.add_filter(API_GRANT_AUTHORIZE_CONTEXT, add_grant_context)
    hooks.add_filter(API_GRANT_AUTHORIZE_FRAGMENTS, add_grant_fragment)
    hooks.add_filter(API_GRANT_AUTHORIZE_CONSTRAINTS, add_grant_constraints)
    hooks.add_filter(PAGE_ADMIN_PAGE_STATE, mark_republish_admin_state)
    hooks.add_filter(PAGE_ADMIN_CAN_MUTATE, prevent_republish_admin_mutation)
    hooks.add_filter(ACCOUNT_PAGE_CONTEXT, add_account_context)
    hooks.add_filter(ACCOUNT_PAGE_SECTIONS, add_account_section)
    _configured = True
