"""Shared helpers for admin controllers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar import Request
from litestar.exceptions import NotAuthorizedException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.services import get_user_permissions
from skrift.admin.navigation import build_admin_nav
from skrift.db.models.user import User
from skrift.db.services import page_service


@dataclass
class PageFormData:
    """Parsed page form data."""

    title: str
    slug: str
    content: str
    is_published: bool
    order: int
    page_type: str
    publish_at: datetime | None
    meta_description: str | None
    og_title: str | None
    og_description: str | None
    og_image: str | None
    meta_robots: str | None


def extract_page_form_data(data: dict) -> PageFormData:
    """Extract and validate page form data from a form submission dict.

    Raises:
        ValueError: If publish_at has an invalid datetime format.
    """
    title = data.get("title", "").strip()
    slug = data.get("slug", "").strip()
    content = data.get("content", "").strip()
    is_published = data.get("is_published") == "on"
    order = int(data.get("order", 0) or 0)

    publish_at_str = data.get("publish_at", "").strip()
    publish_at = None
    if publish_at_str:
        try:
            publish_at = datetime.fromisoformat(publish_at_str)
        except ValueError:
            raise ValueError(f"Invalid publish date format: {publish_at_str}")

    return PageFormData(
        title=title,
        slug=slug,
        content=content,
        is_published=is_published,
        order=order,
        page_type=data.get("type", "page"),
        publish_at=publish_at,
        meta_description=data.get("meta_description", "").strip() or None,
        og_title=data.get("og_title", "").strip() or None,
        og_description=data.get("og_description", "").strip() or None,
        og_image=data.get("og_image", "").strip() or None,
        meta_robots=data.get("meta_robots", "").strip() or None,
    )


async def get_admin_context(request: Request, db_session: AsyncSession) -> dict:
    """Get common admin context including nav and user."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthorizedException("Authentication required")

    result = await db_session.execute(
        select(User).where(User.id == UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise NotAuthorizedException("Invalid user session")

    permissions = await get_user_permissions(db_session, user_id)
    nav_items = await build_admin_nav(
        request.app, permissions, request.url.path
    )

    return {
        "user": user,
        "permissions": permissions,
        "admin_nav": nav_items,
        "current_path": request.url.path,
    }


async def require_page(db_session: AsyncSession, page_id: UUID):
    """Get a page by ID or raise an error if not found."""
    page = await page_service.get_page_by_id(db_session, page_id)
    if not page:
        raise ValueError("Page not found")
    return page


async def check_page_access(
    db_session: AsyncSession,
    request: Request,
    page,
    own_permission: str,
    any_permission: str,
) -> None:
    """Check that the user has access to a specific page.

    Users with `any_permission` (e.g. manage-pages) can access any page.
    Users with `own_permission` (e.g. edit-own-pages) can only access pages they own.

    Raises:
        NotAuthorizedException: If the user doesn't have access.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthorizedException("Authentication required")

    permissions = await get_user_permissions(db_session, user_id)

    # Admins and users with the 'any' permission bypass ownership check
    if "administrator" in permissions.permissions or any_permission in permissions.permissions:
        return

    # Check ownership for users with only the 'own' permission
    if own_permission in permissions.permissions:
        if await page_service.check_page_ownership(db_session, page.id, UUID(user_id)):
            return

    raise NotAuthorizedException("You don't have permission to access this page")
