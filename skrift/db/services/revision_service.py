"""Revision service for page history management."""

from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models import Page, PageRevision


async def create_revision(
    db_session: AsyncSession,
    page: Page,
    user_id: UUID | None = None,
) -> PageRevision:
    """Create a revision snapshot of the current page state.

    Args:
        db_session: Database session
        page: The page to snapshot
        user_id: ID of user making the change (optional)

    Returns:
        The created PageRevision object
    """
    # Get the next revision number for this page
    result = await db_session.execute(
        select(func.coalesce(func.max(PageRevision.revision_number), 0))
        .where(PageRevision.page_id == page.id)
    )
    max_revision = result.scalar()
    next_revision = (max_revision or 0) + 1

    revision = PageRevision(
        page_id=page.id,
        user_id=user_id,
        revision_number=next_revision,
        title=page.title,
        content=page.content,
    )

    db_session.add(revision)
    await db_session.commit()
    await db_session.refresh(revision)

    return revision


async def list_revisions(
    db_session: AsyncSession,
    page_id: UUID,
    limit: int | None = None,
) -> list[PageRevision]:
    """List revisions for a page, newest first.

    Args:
        db_session: Database session
        page_id: The page ID to get revisions for
        limit: Maximum number of revisions to return (None for all)

    Returns:
        List of PageRevision objects ordered by revision_number descending
    """
    query = (
        select(PageRevision)
        .where(PageRevision.page_id == page_id)
        .order_by(PageRevision.revision_number.desc())
    )

    if limit:
        query = query.limit(limit)

    result = await db_session.execute(query)
    return list(result.scalars().all())


async def get_revision(
    db_session: AsyncSession,
    revision_id: UUID,
) -> PageRevision | None:
    """Get a specific revision by ID.

    Args:
        db_session: Database session
        revision_id: The revision ID

    Returns:
        PageRevision object or None if not found
    """
    result = await db_session.execute(
        select(PageRevision).where(PageRevision.id == revision_id)
    )
    return result.scalar_one_or_none()


async def restore_revision(
    db_session: AsyncSession,
    page: Page,
    revision: PageRevision,
    user_id: UUID | None = None,
) -> Page:
    """Restore a page to a previous revision state.

    This creates a new revision first (to preserve current state),
    then updates the page content to match the target revision.

    Args:
        db_session: Database session
        page: The page to restore
        revision: The revision to restore to
        user_id: ID of user performing the restore (optional)

    Returns:
        The updated Page object
    """
    # Create a revision of the current state before restoring
    await create_revision(db_session, page, user_id)

    # Update the page to match the revision
    page.title = revision.title
    page.content = revision.content

    await db_session.commit()
    await db_session.refresh(page)

    return page


async def get_revision_count(
    db_session: AsyncSession,
    page_id: UUID,
) -> int:
    """Get the total number of revisions for a page.

    Args:
        db_session: Database session
        page_id: The page ID

    Returns:
        Number of revisions
    """
    result = await db_session.execute(
        select(func.count(PageRevision.id))
        .where(PageRevision.page_id == page_id)
    )
    return result.scalar() or 0
