from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.db.models.user import User
from skrift.lib.flash import get_flash_messages
from skrift.forms import Form, csrf_field

from twitter.forms import ComposeTweetForm
from twitter.services import feed_service, tweet_service, like_service
from twitter.services.feed_service import get_bookmarked_tweet_ids, get_retweeted_tweet_ids

# Import to trigger hook/role registration at module load
import twitter.hooks  # noqa: F401
import twitter.roles  # noqa: F401


class FeedController(Controller):
    path = "/"

    async def _get_user(self, request: Request, db_session: AsyncSession) -> User | None:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        return result.scalar_one_or_none()

    async def _tweet_context(
        self, request: Request, tweets: list, user: User | None, db_session: AsyncSession
    ) -> dict:
        """Build context for tweet lists: render content and get like/bookmark/retweet state."""
        # Collect source IDs: original tweet ID for retweets, own ID otherwise
        source_ids = [t.retweet_of.id if t.retweet_of else t.id for t in tweets]

        liked_ids: set = set()
        bookmarked_ids: set = set()
        retweeted_ids: set = set()
        if user:
            liked_ids = await like_service.get_liked_tweet_ids(db_session, user.id, source_ids)
            bookmarked_ids = await get_bookmarked_tweet_ids(db_session, user.id, source_ids)
            retweeted_ids = await get_retweeted_tweet_ids(db_session, user.id, source_ids)

        rendered = {}
        for t in tweets:
            source = t.retweet_of if t.retweet_of else t
            rendered[source.id] = await tweet_service.render_tweet_content(source.content)

        return {
            "liked_ids": liked_ids,
            "bookmarked_ids": bookmarked_ids,
            "retweeted_ids": retweeted_ids,
            "rendered_content": rendered,
            "csrf_field": csrf_field(request) if user else Markup(""),
        }

    @get("/")
    async def timeline(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        form = Form(ComposeTweetForm, request)

        if user:
            tweets = await feed_service.get_timeline(db_session, user.id)
        else:
            tweets = await feed_service.get_global_feed(db_session)

        ctx = await self._tweet_context(request, tweets, user, db_session)
        return TemplateResponse(
            "twitter/feed.html",
            context={
                "user": user,
                "tweets": tweets,
                "form": form,
                "flash_messages": flash_messages,
                **ctx,
            },
        )

    @get("/explore")
    async def explore(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        tweets = await feed_service.get_explore_feed(db_session)
        ctx = await self._tweet_context(request, tweets, user, db_session)

        return TemplateResponse(
            "twitter/explore.html",
            context={
                "user": user,
                "tweets": tweets,
                "flash_messages": flash_messages,
                **ctx,
            },
        )

    @get("/search")
    async def search(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        q = request.query_params.get("q", "").strip()[:200]

        tweets = []
        if q:
            tweets = await tweet_service.search_tweets(db_session, q)

        ctx = await self._tweet_context(request, tweets, user, db_session)
        return TemplateResponse(
            "twitter/search.html",
            context={
                "user": user,
                "tweets": tweets,
                "query": q,
                "flash_messages": flash_messages,
                **ctx,
            },
        )

    @get("/bookmarks", guards=[auth_guard])
    async def bookmarks(self, request: Request, db_session: AsyncSession) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)
        tweets = await feed_service.get_bookmarked_tweets(db_session, user.id)
        ctx = await self._tweet_context(request, tweets, user, db_session)

        return TemplateResponse(
            "twitter/bookmarks.html",
            context={
                "user": user,
                "tweets": tweets,
                "flash_messages": flash_messages,
                **ctx,
            },
        )
