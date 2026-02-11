from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.exceptions import NotAuthorizedException
from litestar.response import Template as TemplateResponse, Redirect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard, Permission
from skrift.auth.services import get_user_permissions
from skrift.admin.navigation import build_admin_nav, ADMIN_NAV_TAG
from skrift.db.models.user import User
from skrift.lib.flash import flash_success, flash_error, get_flash_messages

from twitter.models.tweet import Tweet
from twitter.services import tweet_service


class TwitterAdminController(Controller):
    path = "/admin/tweets"
    guards = [auth_guard]

    async def _get_admin_context(self, request: Request, db_session: AsyncSession) -> dict:
        user_id = request.session.get("user_id")
        if not user_id:
            raise NotAuthorizedException("Authentication required")

        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        user = result.scalar_one_or_none()
        if not user:
            raise NotAuthorizedException("Invalid user session")

        permissions = await get_user_permissions(db_session, user_id)
        nav_items = await build_admin_nav(request.app, permissions, request.url.path)

        return {
            "user": user,
            "permissions": permissions,
            "admin_nav": nav_items,
            "current_path": request.url.path,
        }

    @get(
        "/",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("moderate-tweets")],
        opt={"label": "Tweets", "icon": "message-circle", "order": 30},
    )
    async def list_tweets(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        ctx = await self._get_admin_context(request, db_session)

        result = await db_session.execute(
            select(Tweet)
            .where(Tweet.is_deleted == False)
            .order_by(Tweet.created_at.desc())
            .limit(100)
        )
        tweets = list(result.scalars().all())

        # Render content for display
        rendered = {}
        for t in tweets:
            rendered[t.id] = await tweet_service.render_tweet_content(t.content)

        flash_messages = get_flash_messages(request)
        return TemplateResponse(
            "twitter/admin/moderation.html",
            context={
                "flash_messages": flash_messages,
                "tweets": tweets,
                "rendered_content": rendered,
                **ctx,
            },
        )

    @post(
        "/{tweet_id:uuid}/delete",
        guards=[auth_guard, Permission("moderate-tweets")],
    )
    async def delete_tweet(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> Redirect:
        result = await tweet_service.soft_delete_tweet(db_session, tweet_id)
        if result:
            flash_success(request, "Tweet deleted by moderator")
        else:
            flash_error(request, "Tweet not found")
        return Redirect(path="/admin/tweets")
