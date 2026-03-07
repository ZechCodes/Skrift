"""Shared page orchestration for admin controllers."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from skrift.admin.helpers import PageFormData
from skrift.db.models import Page
from skrift.db.services import page_service
from skrift.db.services.asset_service import sync_page_assets


def _resolve_featured_asset_id(featured_asset_id: str | None) -> UUID | None:
    return UUID(featured_asset_id) if featured_asset_id else None


def _resolve_asset_ids(asset_ids: list[str]) -> list[UUID]:
    return [UUID(asset_id) for asset_id in asset_ids]


def _get_permissions(permission_source: Collection[str] | object) -> Collection[str]:
    permissions = getattr(permission_source, "permissions", permission_source)
    if not isinstance(permissions, Collection):
        raise TypeError("permissions must be a collection of strings")
    return permissions


async def list_pages_for_admin(
    db_session: AsyncSession,
    *,
    page_type_name: str,
    user_id: UUID,
    permissions: Collection[str] | object,
    manage_permission: str,
) -> list[Page]:
    """List admin pages for the current user, applying ownership rules."""
    query = (
        select(Page)
        .where(Page.type == page_type_name)
        .options(selectinload(Page.user))
        .order_by(Page.order.asc(), Page.created_at.desc())
    )

    permission_values = _get_permissions(permissions)
    if "administrator" not in permission_values and manage_permission not in permission_values:
        query = query.where(Page.user_id == user_id)

    result = await db_session.execute(query)
    return list(result.scalars().all())


async def create_typed_page(
    db_session: AsyncSession,
    *,
    form: PageFormData,
    user_id: UUID,
    page_type_name: str,
) -> Page:
    """Create a page and synchronize attached assets."""
    page = await page_service.create_page(
        db_session,
        slug=form.slug,
        title=form.title,
        content=form.content,
        is_published=form.is_published,
        published_at=datetime.now(UTC) if form.is_published else None,
        order=form.order,
        publish_at=form.publish_at,
        meta_description=form.meta_description,
        og_title=form.og_title,
        og_description=form.og_description,
        og_image=form.og_image,
        meta_robots=form.meta_robots,
        user_id=user_id,
        page_type=page_type_name,
        featured_asset_id=_resolve_featured_asset_id(form.featured_asset_id),
    )

    if form.asset_ids:
        await sync_page_assets(db_session, page.id, _resolve_asset_ids(form.asset_ids))

    return page


async def update_typed_page(
    db_session: AsyncSession,
    *,
    page: Page,
    form: PageFormData,
    user_id: UUID | None,
    page_type_name: str,
) -> Page | None:
    """Update a page and synchronize attached assets."""
    published_at = page.published_at
    if form.is_published and not page.is_published:
        published_at = datetime.now(UTC)

    updated_page = await page_service.update_page(
        db_session,
        page_id=page.id,
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
        page_type=page_type_name,
        user_id=user_id,
        featured_asset_id=_resolve_featured_asset_id(form.featured_asset_id),
    )

    await sync_page_assets(db_session, page.id, _resolve_asset_ids(form.asset_ids))
    return updated_page
