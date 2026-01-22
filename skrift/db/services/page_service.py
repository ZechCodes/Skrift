"""Page service for CRUD operations on pages."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models import Page


async def list_pages(
    db_session: AsyncSession,
    published_only: bool = False,
    user_id: UUID | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Page]:
    """List pages with optional filtering.

    Args:
        db_session: Database session
        published_only: Only return published pages
        user_id: Filter by user ID (author)
        limit: Maximum number of results
        offset: Number of results to skip

    Returns:
        List of Page objects
    """
    query = select(Page)

    # Build filters
    filters = []
    if published_only:
        filters.append(Page.is_published == True)
    if user_id:
        filters.append(Page.user_id == user_id)

    if filters:
        query = query.where(and_(*filters))

    # Order by published date (newest first), then created date
    query = query.order_by(Page.published_at.desc().nullslast(), Page.created_at.desc())

    # Apply pagination
    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)

    result = await db_session.execute(query)
    return list(result.scalars().all())


async def get_page_by_slug(
    db_session: AsyncSession,
    slug: str,
    published_only: bool = False,
) -> Page | None:
    """Get a single page by slug.

    Args:
        db_session: Database session
        slug: Page slug
        published_only: Only return if published

    Returns:
        Page object or None if not found
    """
    query = select(Page).where(Page.slug == slug)

    if published_only:
        query = query.where(Page.is_published == True)

    result = await db_session.execute(query)
    return result.scalar_one_or_none()


async def get_page_by_id(
    db_session: AsyncSession,
    page_id: UUID,
) -> Page | None:
    """Get a single page by ID.

    Args:
        db_session: Database session
        page_id: Page UUID

    Returns:
        Page object or None if not found
    """
    result = await db_session.execute(select(Page).where(Page.id == page_id))
    return result.scalar_one_or_none()


async def create_page(
    db_session: AsyncSession,
    slug: str,
    title: str,
    content: str = "",
    is_published: bool = False,
    published_at: datetime | None = None,
    user_id: UUID | None = None,
) -> Page:
    """Create a new page.

    Args:
        db_session: Database session
        slug: Unique page slug
        title: Page title
        content: Page content (HTML or markdown)
        is_published: Whether page is published
        published_at: Publication timestamp
        user_id: Author user ID (optional)

    Returns:
        Created Page object
    """
    page = Page(
        slug=slug,
        title=title,
        content=content,
        is_published=is_published,
        published_at=published_at,
        user_id=user_id,
    )
    db_session.add(page)
    await db_session.commit()
    await db_session.refresh(page)
    return page


async def update_page(
    db_session: AsyncSession,
    page_id: UUID,
    slug: str | None = None,
    title: str | None = None,
    content: str | None = None,
    is_published: bool | None = None,
    published_at: datetime | None = None,
) -> Page | None:
    """Update an existing page.

    Args:
        db_session: Database session
        page_id: Page UUID to update
        slug: New slug (optional)
        title: New title (optional)
        content: New content (optional)
        is_published: New published status (optional)
        published_at: New publication timestamp (optional)

    Returns:
        Updated Page object or None if not found
    """
    page = await get_page_by_id(db_session, page_id)
    if not page:
        return None

    if slug is not None:
        page.slug = slug
    if title is not None:
        page.title = title
    if content is not None:
        page.content = content
    if is_published is not None:
        page.is_published = is_published
    if published_at is not None:
        page.published_at = published_at

    await db_session.commit()
    await db_session.refresh(page)
    return page


async def delete_page(
    db_session: AsyncSession,
    page_id: UUID,
) -> bool:
    """Delete a page.

    Args:
        db_session: Database session
        page_id: Page UUID to delete

    Returns:
        True if deleted, False if not found
    """
    page = await get_page_by_id(db_session, page_id)
    if not page:
        return False

    await db_session.delete(page)
    await db_session.commit()
    return True


async def check_page_ownership(
    db_session: AsyncSession,
    page_id: UUID,
    user_id: UUID,
) -> bool:
    """Check if a user owns a page.

    Args:
        db_session: Database session
        page_id: Page UUID to check
        user_id: User UUID to check ownership

    Returns:
        True if user owns the page, False otherwise
    """
    page = await get_page_by_id(db_session, page_id)
    if not page:
        return False
    return page.user_id == user_id
