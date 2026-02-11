from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse, Redirect
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.db.models.user import User
from skrift.db.services.setting_service import get_cached_site_name, get_cached_site_base_url
from skrift.lib.flash import flash_success, flash_error, get_flash_messages
from skrift.lib.hooks import hooks
from skrift.lib.seo import SEOMeta, OpenGraphMeta
from skrift.forms import verify_csrf, csrf_field
from twitter.hooks import TWEET_SEO_META, TWEET_OG_META
from twitter.models.tweet import Tweet
from twitter.services import tweet_service, like_service, follow_service, feed_service


class ProfileController(Controller):
    path = "/profile"

    async def _get_user(self, request: Request, db_session: AsyncSession) -> User | None:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        return result.scalar_one_or_none()

    async def _get_profile_user(self, db_session: AsyncSession, profile_id: UUID) -> User | None:
        result = await db_session.execute(select(User).where(User.id == profile_id))
        return result.scalar_one_or_none()

    @get("/{profile_id:uuid}")
    async def profile(self, request: Request, db_session: AsyncSession, profile_id: UUID) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        profile_user = await self._get_profile_user(db_session, profile_id)

        if not profile_user:
            return TemplateResponse(
                "twitter/profile_not_found.html",
                context={"user": user, "flash_messages": flash_messages},
                status_code=404,
            )

        tweets = await tweet_service.get_user_tweets(db_session, profile_id)
        follower_count = await follow_service.get_follower_count(db_session, profile_id)
        following_count = await follow_service.get_following_count(db_session, profile_id)

        # Tweet count
        tweet_count_result = await db_session.execute(
            select(func.count()).select_from(Tweet).where(
                Tweet.user_id == profile_id,
                Tweet.is_deleted == False,
                Tweet.parent_id.is_(None),
            )
        )
        tweet_count = tweet_count_result.scalar() or 0

        is_following = False
        if user and user.id != profile_id:
            is_following = await follow_service.is_following(db_session, user.id, profile_id)

        # Tweet interaction state
        tweet_ids = [t.id for t in tweets]
        liked_ids: set = set()
        bookmarked_ids: set = set()
        if user:
            liked_ids = await like_service.get_liked_tweet_ids(db_session, user.id, tweet_ids)
            bookmarked_ids = await feed_service.get_bookmarked_tweet_ids(db_session, user.id, tweet_ids)

        rendered = {}
        for t in tweets:
            rendered[t.id] = await tweet_service.render_tweet_content(t.content)

        # SEO
        site_name = get_cached_site_name()
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
        profile_name = profile_user.name or "User"

        seo_meta = SEOMeta(
            title=f"{profile_name} | {site_name}",
            description=f"{profile_name} has {tweet_count} tweets and {follower_count} followers.",
            canonical_url=f"{base_url}/profile/{profile_id}",
            robots=None,
        )
        seo_meta = await hooks.apply_filters(TWEET_SEO_META, seo_meta, profile_user)

        og_meta = OpenGraphMeta(
            title=f"{profile_name} | {site_name}",
            description=f"{profile_name} has {tweet_count} tweets and {follower_count} followers.",
            image=profile_user.picture_url,
            url=f"{base_url}/profile/{profile_id}",
            site_name=site_name,
            type="profile",
        )
        og_meta = await hooks.apply_filters(TWEET_OG_META, og_meta, profile_user)

        return TemplateResponse(
            "twitter/profile.html",
            context={
                "user": user,
                "profile_user": profile_user,
                "tweets": tweets,
                "tweet_count": tweet_count,
                "follower_count": follower_count,
                "following_count": following_count,
                "is_following": is_following,
                "liked_ids": liked_ids,
                "bookmarked_ids": bookmarked_ids,
                "rendered_content": rendered,
                "csrf_field": csrf_field(request) if user else "",
                "seo_meta": seo_meta,
                "og_meta": og_meta,
                "flash_messages": flash_messages,
            },
        )

    @post("/{profile_id:uuid}/follow", guards=[auth_guard])
    async def follow(self, request: Request, db_session: AsyncSession, profile_id: UUID) -> Redirect:
        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path=f"/profile/{profile_id}")
        user = await self._get_user(request, db_session)
        result = await follow_service.toggle_follow(db_session, user.id, profile_id)
        if result:
            flash_success(request, "Followed!")
        else:
            flash_success(request, "Unfollowed")
        return Redirect(path=f"/profile/{profile_id}")

    @get("/{profile_id:uuid}/followers")
    async def followers(self, request: Request, db_session: AsyncSession, profile_id: UUID) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        profile_user = await self._get_profile_user(db_session, profile_id)

        if not profile_user:
            return TemplateResponse(
                "twitter/profile_not_found.html",
                context={"user": user, "flash_messages": flash_messages},
                status_code=404,
            )

        follower_list = await follow_service.get_followers(db_session, profile_id)

        return TemplateResponse(
            "twitter/followers.html",
            context={
                "user": user,
                "profile_user": profile_user,
                "users_list": follower_list,
                "list_type": "Followers",
                "flash_messages": flash_messages,
            },
        )

    @get("/{profile_id:uuid}/following")
    async def following(self, request: Request, db_session: AsyncSession, profile_id: UUID) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        profile_user = await self._get_profile_user(db_session, profile_id)

        if not profile_user:
            return TemplateResponse(
                "twitter/profile_not_found.html",
                context={"user": user, "flash_messages": flash_messages},
                status_code=404,
            )

        following_list = await follow_service.get_following(db_session, profile_id)

        return TemplateResponse(
            "twitter/following.html",
            context={
                "user": user,
                "profile_user": profile_user,
                "users_list": following_list,
                "list_type": "Following",
                "flash_messages": flash_messages,
            },
        )
