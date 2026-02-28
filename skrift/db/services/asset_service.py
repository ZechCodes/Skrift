"""Asset storage service — upload, delete, list, URL resolution."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.asset import Asset
from skrift.db.models.page_asset import page_assets
from skrift.lib.hooks import (
    AFTER_ASSET_DELETE,
    AFTER_ASSET_UPLOAD,
    ASSET_UPLOAD_DATA,
    ASSET_UPLOAD_KEY,
    BEFORE_ASSET_DELETE,
    BEFORE_ASSET_UPLOAD,
    hooks,
)
from skrift.lib.storage.manager import StorageManager


class UploadTooLargeError(Exception):
    """Raised when an upload exceeds the configured size limit."""


async def upload_asset(
    db_session: AsyncSession,
    storage: StorageManager,
    filename: str,
    data: bytes,
    content_type: str,
    store: str | None = None,
    folder: str = "",
    user_id: UUID | None = None,
) -> Asset:
    """Upload a file and create an Asset record.

    Deduplicates by content hash — if the same bytes already exist in the
    store the backend ``put`` is skipped and only a new DB row is created.
    """
    store_name = store or storage.default_store
    backend = await storage.get(store_name)

    # Enforce size limit
    store_cfg = storage._config.stores.get(store_name)
    if store_cfg and len(data) > store_cfg.max_upload_size:
        raise UploadTooLargeError(
            f"File size {len(data)} exceeds limit {store_cfg.max_upload_size}"
        )

    # Apply data filter hook
    data = await hooks.apply_filters(ASSET_UPLOAD_DATA, data, filename=filename)

    # Compute content hash and build key
    content_hash = hashlib.sha256(data).hexdigest()
    key = content_hash

    # Apply key filter hook
    key = await hooks.apply_filters(ASSET_UPLOAD_KEY, key, filename=filename, content_hash=content_hash)

    # Fire before-upload action
    await hooks.do_action(BEFORE_ASSET_UPLOAD, filename=filename, key=key, store=store_name)

    # Dedup: skip backend write if identical content already stored
    existing = await db_session.execute(
        select(Asset).where(
            and_(Asset.store == store_name, Asset.content_hash == content_hash)
        ).limit(1)
    )
    if not existing.scalar_one_or_none():
        await backend.put(key, data, content_type)

    # Create Asset row
    asset = Asset(
        key=key,
        store=store_name,
        content_hash=content_hash,
        filename=filename,
        content_type=content_type,
        size=len(data),
        folder=folder,
        user_id=user_id,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)

    # Fire after-upload action
    await hooks.do_action(AFTER_ASSET_UPLOAD, asset)

    return asset


async def delete_asset(
    db_session: AsyncSession,
    storage: StorageManager,
    asset_id: UUID,
) -> bool:
    """Delete an asset. Removes backend file only when no other rows reference it."""
    result = await db_session.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        return False

    await hooks.do_action(BEFORE_ASSET_DELETE, asset)

    store_name = asset.store
    key = asset.key
    content_hash = asset.content_hash

    await db_session.delete(asset)
    await db_session.commit()

    # Check remaining references
    count_result = await db_session.execute(
        select(func.count()).select_from(Asset).where(
            and_(Asset.store == store_name, Asset.content_hash == content_hash)
        )
    )
    remaining = count_result.scalar() or 0

    if remaining == 0:
        backend = await storage.get(store_name)
        await backend.delete(key)

    await hooks.do_action(AFTER_ASSET_DELETE, asset_id=asset_id, key=key, store=store_name)

    return True


async def get_asset_url(storage: StorageManager, asset: Asset) -> str:
    """Return the public/signed URL for an asset."""
    backend = await storage.get(asset.store)
    return await backend.get_url(asset.key)


async def list_assets(
    db_session: AsyncSession,
    store: str | None = None,
    folder: str | None = None,
    content_type_prefix: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Asset]:
    """List assets with optional filtering."""
    query = select(Asset)

    filters = []
    if store is not None:
        filters.append(Asset.store == store)
    if folder is not None:
        filters.append(Asset.folder == folder)
    if content_type_prefix is not None:
        filters.append(Asset.content_type.startswith(content_type_prefix))
    if filters:
        query = query.where(and_(*filters))

    query = query.order_by(Asset.created_at.desc())
    if offset:
        query = query.offset(offset)
    query = query.limit(limit)

    result = await db_session.execute(query)
    return list(result.scalars().all())


async def count_assets(
    db_session: AsyncSession,
    store: str | None = None,
    folder: str | None = None,
    content_type_prefix: str | None = None,
) -> int:
    """Count assets matching the given filters."""
    query = select(func.count()).select_from(Asset)

    filters = []
    if store is not None:
        filters.append(Asset.store == store)
    if folder is not None:
        filters.append(Asset.folder == folder)
    if content_type_prefix is not None:
        filters.append(Asset.content_type.startswith(content_type_prefix))
    if filters:
        query = query.where(and_(*filters))

    result = await db_session.execute(query)
    return result.scalar() or 0


async def get_page_asset_ids(
    db_session: AsyncSession,
    page_id: UUID,
) -> list[UUID]:
    """Return current asset IDs attached to a page."""
    result = await db_session.execute(
        select(page_assets.c.asset_id).where(page_assets.c.page_id == page_id)
    )
    return list(result.scalars().all())


async def sync_page_assets(
    db_session: AsyncSession,
    page_id: UUID,
    asset_ids: list[UUID],
) -> None:
    """Sync the page_assets rows so the page is linked to exactly `asset_ids`."""
    current = set(await get_page_asset_ids(db_session, page_id))
    desired = set(asset_ids)

    to_remove = current - desired
    to_add = desired - current

    if to_remove:
        await db_session.execute(
            delete(page_assets).where(
                and_(
                    page_assets.c.page_id == page_id,
                    page_assets.c.asset_id.in_(to_remove),
                )
            )
        )

    if to_add:
        await db_session.execute(
            page_assets.insert(),
            [{"page_id": page_id, "asset_id": aid} for aid in to_add],
        )

    if to_remove or to_add:
        await db_session.commit()
