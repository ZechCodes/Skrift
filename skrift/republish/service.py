"""Persistence helpers for republished pages."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.db.models import APIKey, Page, PageRepublish, RepublishSiteLink
from skrift.db.services import page_service
from skrift.republish import REPUBLISH_PERMISSION


def origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def slug_for_canonical_url(canonical_url: str) -> str:
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
    return f"reposts/{digest}"


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def get_republish_by_canonical_url(
    db_session: AsyncSession,
    canonical_url: str,
) -> PageRepublish | None:
    result = await db_session.execute(
        select(PageRepublish)
        .options(selectinload(PageRepublish.page))
        .where(PageRepublish.canonical_url == canonical_url)
    )
    return result.scalar_one_or_none()


async def upsert_republished_page(
    db_session: AsyncSession,
    *,
    user_id: UUID,
    canonical_url: str,
    source_origin: str,
    page_type: str,
    post_behavior: str,
    title: str,
    content: str,
    summary: str = "",
    author_name: str = "",
    image_url: str = "",
    tags: list[str] | None = None,
    source_updated_at: datetime | None = None,
) -> tuple[PageRepublish, bool]:
    existing = await get_republish_by_canonical_url(db_session, canonical_url)
    is_published = post_behavior == "publish"
    published_at = datetime.now(UTC) if is_published else None

    if existing is None:
        page = await page_service.create_page(
            db_session,
            slug=slug_for_canonical_url(canonical_url),
            title=title,
            content=content,
            is_published=is_published,
            published_at=published_at,
            user_id=user_id,
            page_type=page_type,
            meta_description=summary or None,
            og_title=title,
            og_description=summary or None,
            og_image=image_url or None,
        )
        existing = PageRepublish(
            page_id=page.id,
            canonical_url=canonical_url,
            source_origin=source_origin,
        )
        db_session.add(existing)
        created = True
    else:
        await page_service.update_page(
            db_session,
            page_id=existing.page_id,
            title=title,
            content=content,
            is_published=is_published,
            published_at=published_at,
            meta_description=summary or None,
            og_title=title,
            og_description=summary or None,
            og_image=image_url or None,
            page_type=page_type,
            user_id=user_id,
        )
        existing.source_origin = source_origin
        created = False

    existing.source_updated_at = source_updated_at
    existing.author_name = author_name or None
    existing.image_url = image_url or None
    existing.tags_json = json.dumps(tags or [], sort_keys=True)
    await db_session.commit()
    await db_session.refresh(existing)
    return existing, created


async def apply_republish_delete_behavior(
    db_session: AsyncSession,
    republish: PageRepublish,
    *,
    behavior: str,
) -> str:
    if behavior == "ignore":
        return "ignored"
    if behavior == "delete":
        await page_service.delete_page(db_session, republish.page_id)
        return "deleted"

    await page_service.update_page(
        db_session,
        page_id=republish.page_id,
        is_published=False,
    )
    return "unpublished"


async def list_outbound_links(
    db_session: AsyncSession,
    *,
    user_id: UUID,
) -> list[RepublishSiteLink]:
    result = await db_session.execute(
        select(RepublishSiteLink)
        .where(RepublishSiteLink.user_id == user_id)
        .order_by(RepublishSiteLink.created_at.desc())
    )
    return list(result.scalars().all())


async def save_outbound_link(
    db_session: AsyncSession,
    *,
    user_id: UUID,
    site_url: str,
    site_name: str = "",
) -> RepublishSiteLink:
    target_origin = origin_from_url(site_url)
    result = await db_session.execute(
        select(RepublishSiteLink)
        .where(RepublishSiteLink.user_id == user_id)
        .where(RepublishSiteLink.target_origin == target_origin)
    )
    link = result.scalar_one_or_none()
    if link is None:
        link = RepublishSiteLink(
            user_id=user_id,
            site_url=site_url,
            site_name=site_name or None,
            target_origin=target_origin,
        )
        db_session.add(link)
    else:
        link.site_url = site_url
        link.site_name = site_name or None

    await db_session.commit()
    await db_session.refresh(link)
    return link


async def list_inbound_publishers(
    db_session: AsyncSession,
    *,
    user_id: UUID,
) -> list[dict[str, object]]:
    post_counts_result = await db_session.execute(
        select(PageRepublish.source_origin, func.count(PageRepublish.id))
        .join(Page, Page.id == PageRepublish.page_id)
        .where(Page.user_id == user_id)
        .group_by(PageRepublish.source_origin)
    )
    post_counts = {
        str(source_origin): int(count)
        for source_origin, count in post_counts_result.all()
    }

    key_result = await db_session.execute(
        select(APIKey)
        .where(APIKey.user_id == user_id)
        .where(APIKey.grant_source == "api-grant")
    )
    publishers: dict[str, dict[str, object]] = {
        source_origin: {
            "source_origin": source_origin,
            "service_name": "",
            "key_prefix": "",
            "post_count": count,
        }
        for source_origin, count in post_counts.items()
    }

    for api_key in key_result.scalars():
        if REPUBLISH_PERMISSION not in api_key.scoped_permission_list:
            continue
        constraints = api_key.constraint_data.get("republish")
        if not isinstance(constraints, dict):
            continue
        source_origin = str(constraints.get("source_origin", "")).strip()
        if not source_origin:
            continue
        entry = publishers.setdefault(
            source_origin,
            {
                "source_origin": source_origin,
                "service_name": "",
                "key_prefix": "",
                "post_count": 0,
            },
        )
        entry["service_name"] = api_key.service_name or api_key.display_name
        entry["key_prefix"] = api_key.key_prefix

    return sorted(
        publishers.values(),
        key=lambda item: (str(item.get("service_name") or ""), str(item["source_origin"])),
    )
