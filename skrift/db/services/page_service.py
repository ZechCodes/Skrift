"""Page service for CRUD operations on pages."""

from datetime import datetime, UTC
from typing import Literal
from uuid import UUID

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models import Page
from skrift.db.services import revision_service
from skrift.lib.hooks import hooks, BEFORE_PAGE_SAVE, AFTER_PAGE_SAVE, BEFORE_PAGE_DELETE, AFTER_PAGE_DELETE


OrderBy = Literal["order", "created", "published", "title"]


async def list_pages(
    db_session: AsyncSession,
    published_only: bool = False,
    user_id: UUID | None = None,
    limit: int | None = None,
    offset: int = 0,
    order_by: OrderBy = "order",
) -> list[Page]:
    """List pages with optional filtering.

    Args:
        db_session: Database session
        published_only: Only return published pages (respects scheduling)
        user_id: Filter by user ID (author)
        limit: Maximum number of results
        offset: Number of results to skip
        order_by: Sort order - "order" (default), "created", "published", "title"

    Returns:
        List of Page objects
    """
    query = select(Page)

    # Build filters
    filters = []
    if published_only:
        now = datetime.now(UTC)
        filters.append(Page.is_published == True)
        # Respect scheduling: either no publish_at set, or publish_at is in the past
        filters.append(or_(Page.publish_at.is_(None), Page.publish_at <= now))
    if user_id:
        filters.append(Page.user_id == user_id)

    if filters:
        query = query.where(and_(*filters))

    # Apply ordering
    if order_by == "order":
        query = query.order_by(Page.order.asc(), Page.created_at.desc())
    elif order_by == "created":
        query = query.order_by(Page.created_at.desc())
    elif order_by == "published":
        query = query.order_by(Page.published_at.desc().nullslast(), Page.created_at.desc())
    elif order_by == "title":
        query = query.order_by(Page.title.asc())

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
        published_only: Only return if published (respects scheduling)

    Returns:
        Page object or None if not found
    """
    query = select(Page).where(Page.slug == slug)

    if published_only:
        now = datetime.now(UTC)
        query = query.where(Page.is_published == True)
        # Respect scheduling: either no publish_at set, or publish_at is in the past
        query = query.where(or_(Page.publish_at.is_(None), Page.publish_at <= now))

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
    order: int = 0,
    publish_at: datetime | None = None,
    meta_description: str | None = None,
    og_title: str | None = None,
    og_description: str | None = None,
    og_image: str | None = None,
    meta_robots: str | None = None,
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
        order: Display order (lower numbers first)
        publish_at: Scheduled publish datetime
        meta_description: SEO meta description
        og_title: OpenGraph title override
        og_description: OpenGraph description override
        og_image: OpenGraph image URL
        meta_robots: Meta robots directive

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
        order=order,
        publish_at=publish_at,
        meta_description=meta_description,
        og_title=og_title,
        og_description=og_description,
        og_image=og_image,
        meta_robots=meta_robots,
    )

    # Fire before_page_save action (is_new=True for creates)
    await hooks.do_action(BEFORE_PAGE_SAVE, page, is_new=True)

    db_session.add(page)
    await db_session.commit()
    await db_session.refresh(page)

    # Fire after_page_save action
    await hooks.do_action(AFTER_PAGE_SAVE, page, is_new=True)

    return page


_UNSET = object()  # Sentinel for distinguishing None from "not provided"


async def update_page(
    db_session: AsyncSession,
    page_id: UUID,
    slug: str | None = None,
    title: str | None = None,
    content: str | None = None,
    is_published: bool | None = None,
    published_at: datetime | None = None,
    order: int | None = None,
    publish_at: datetime | None | object = _UNSET,
    meta_description: str | None | object = _UNSET,
    og_title: str | None | object = _UNSET,
    og_description: str | None | object = _UNSET,
    og_image: str | None | object = _UNSET,
    meta_robots: str | None | object = _UNSET,
    create_revision: bool = True,
    user_id: UUID | None = None,
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
        order: New display order (optional)
        publish_at: New scheduled publish datetime (optional, use None to clear)
        meta_description: New SEO meta description (optional, use None to clear)
        og_title: New OpenGraph title (optional, use None to clear)
        og_description: New OpenGraph description (optional, use None to clear)
        og_image: New OpenGraph image URL (optional, use None to clear)
        meta_robots: New meta robots directive (optional, use None to clear)
        create_revision: Whether to create a revision before updating (default True)
        user_id: ID of user making the change (for revision tracking)

    Returns:
        Updated Page object or None if not found
    """
    page = await get_page_by_id(db_session, page_id)
    if not page:
        return None

    # Create revision before making changes (if title or content is changing)
    if create_revision and (title is not None or content is not None):
        # Only create revision if title or content actually differs
        title_changed = title is not None and title != page.title
        content_changed = content is not None and content != page.content
        if title_changed or content_changed:
            await revision_service.create_revision(db_session, page, user_id)

    # Fire before_page_save action (is_new=False for updates)
    await hooks.do_action(BEFORE_PAGE_SAVE, page, is_new=False)

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
    if order is not None:
        page.order = order
    if publish_at is not _UNSET:
        page.publish_at = publish_at
    if meta_description is not _UNSET:
        page.meta_description = meta_description
    if og_title is not _UNSET:
        page.og_title = og_title
    if og_description is not _UNSET:
        page.og_description = og_description
    if og_image is not _UNSET:
        page.og_image = og_image
    if meta_robots is not _UNSET:
        page.meta_robots = meta_robots

    await db_session.commit()
    await db_session.refresh(page)

    # Fire after_page_save action
    await hooks.do_action(AFTER_PAGE_SAVE, page, is_new=False)

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

    # Fire before_page_delete action
    await hooks.do_action(BEFORE_PAGE_DELETE, page)

    await db_session.delete(page)
    await db_session.commit()

    # Fire after_page_delete action
    await hooks.do_action(AFTER_PAGE_DELETE, page)

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
