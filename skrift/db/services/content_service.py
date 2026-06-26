"""Service for persisting code-declared content area values."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models import ContentAreaRecord


async def get_content_data(db_session: AsyncSession, key: str) -> dict[str, Any]:
    """Return the stored values for a content area, or an empty dict if unset."""
    result = await db_session.execute(
        select(ContentAreaRecord).where(ContentAreaRecord.key == key)
    )
    record = result.scalar_one_or_none()
    return record.data if record and record.data else {}


async def save_content_data(
    db_session: AsyncSession, key: str, data: dict[str, Any]
) -> ContentAreaRecord:
    """Create or update the stored values for a content area."""
    result = await db_session.execute(
        select(ContentAreaRecord).where(ContentAreaRecord.key == key)
    )
    record = result.scalar_one_or_none()

    if record is None:
        record = ContentAreaRecord(key=key, data=data)
        db_session.add(record)
    else:
        record.data = data

    await db_session.commit()
    await db_session.refresh(record)
    return record
