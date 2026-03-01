"""Shared helpers for public-facing controllers."""

from uuid import UUID

from litestar import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.session_keys import SESSION_USER_ID
from skrift.db.models.user import User
from skrift.db.services.setting_service import get_cached_site_theme
from skrift.lib.hooks import RESOLVE_THEME, apply_filters


async def get_user_context(request: Request, db_session: AsyncSession) -> dict:
    """Get user data for template context if logged in."""
    user_id = request.session.get(SESSION_USER_ID)
    if not user_id:
        return {"user": None}

    result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    return {"user": user}


async def resolve_theme(request: Request) -> str:
    """Resolve the active theme for this request via filter hook."""
    theme_name = get_cached_site_theme()
    return await apply_filters(RESOLVE_THEME, theme_name, request)
