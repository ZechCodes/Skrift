from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Template as TemplateResponse, Redirect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import auth_guard
from skrift.db.models.user import User
from skrift.db.services.setting_service import get_cached_site_name, get_cached_site_base_url
from skrift.lib.flash import flash_success, flash_error, get_flash_messages
from skrift.lib.hooks import hooks
from skrift.lib.notifications import notify_user, notify_broadcast
from skrift.lib.seo import SEOMeta, OpenGraphMeta
from skrift.forms import Form, verify_csrf, csrf_field

from twitter.forms import ComposeTweetForm, ReplyForm
from twitter.hooks import TWEET_SEO_META, TWEET_OG_META
from twitter.services import tweet_service, like_service, feed_service
from twitter.services.feed_service import get_retweeted_tweet_ids


class TweetController(Controller):
    path = "/tweet"

    async def _get_user(self, request: Request, db_session: AsyncSession) -> User | None:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        result = await db_session.execute(select(User).where(User.id == UUID(user_id)))
        return result.scalar_one_or_none()

    @post("/compose", guards=[auth_guard])
    async def compose(self, request: Request, db_session: AsyncSession) -> Redirect:
        user = await self._get_user(request, db_session)
        form = Form(ComposeTweetForm, request)

        if await form.validate():
            tweet = await tweet_service.create_tweet(db_session, user.id, form.data.content)
            flash_success(request, "Tweet posted!")
            rendered = await tweet_service.render_tweet_content(tweet.content)
            await notify_broadcast(
                "new_tweet",
                tweet_id=str(tweet.id),
                user_id=str(tweet.user_id),
                user_name=tweet.user.name or "User",
                user_picture_url=tweet.user.picture_url,
                content_html=str(rendered),
                created_at=tweet.created_at.strftime("%b %d"),
            )
        else:
            errors = "; ".join(form.errors.values())
            flash_error(request, f"Could not post tweet: {errors}")

        return Redirect(path="/")

    @get("/{tweet_id:uuid}")
    async def detail(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> TemplateResponse:
        user = await self._get_user(request, db_session)
        flash_messages = get_flash_messages(request)

        tweet = await tweet_service.get_tweet_by_id(db_session, tweet_id)
        if not tweet:
            flash_error(request, "Tweet not found")
            return Redirect(path="/")

        replies = await tweet_service.get_tweet_replies(db_session, tweet_id)
        reply_form = Form(ReplyForm, request)
        rendered_content = await tweet_service.render_tweet_content(tweet.content)

        # Render reply content
        rendered_replies = {}
        for r in replies:
            rendered_replies[r.id] = await tweet_service.render_tweet_content(r.content)

        # Check like/bookmark/retweet state
        liked_ids: set = set()
        bookmarked_ids: set = set()
        retweeted_ids: set = set()
        all_ids = [tweet.id] + [r.id for r in replies]
        if user:
            liked_ids = await like_service.get_liked_tweet_ids(db_session, user.id, all_ids)
            bookmarked_ids = await feed_service.get_bookmarked_tweet_ids(db_session, user.id, all_ids)
            retweeted_ids = await get_retweeted_tweet_ids(db_session, user.id, all_ids)

        # SEO
        site_name = get_cached_site_name()
        base_url = get_cached_site_base_url() or str(request.base_url).rstrip("/")
        author_name = tweet.user.name or "User"
        description = tweet.content[:160]
        short_content = tweet.content[:60]
        if len(tweet.content) > 60:
            short_content += "..."

        seo_meta = SEOMeta(
            title=f'{author_name} on {site_name}: "{short_content}"',
            description=description,
            canonical_url=f"{base_url}/tweet/{tweet.id}",
            robots=None,
        )
        seo_meta = await hooks.apply_filters(TWEET_SEO_META, seo_meta, tweet)

        og_meta = OpenGraphMeta(
            title=f'{author_name}: "{short_content}"',
            description=description,
            image=tweet.user.picture_url,
            url=f"{base_url}/tweet/{tweet.id}",
            site_name=site_name,
            type="article",
        )
        og_meta = await hooks.apply_filters(TWEET_OG_META, og_meta, tweet)

        return TemplateResponse(
            "twitter/tweet.html",
            context={
                "user": user,
                "tweet": tweet,
                "replies": replies,
                "reply_form": reply_form,
                "rendered_content": {tweet.id: rendered_content, **rendered_replies},
                "liked_ids": liked_ids,
                "bookmarked_ids": bookmarked_ids,
                "retweeted_ids": retweeted_ids,
                "csrf_field": csrf_field(request) if user else "",
                "seo_meta": seo_meta,
                "og_meta": og_meta,
                "flash_messages": flash_messages,
            },
        )

    @post("/{tweet_id:uuid}/reply", guards=[auth_guard])
    async def reply(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> Redirect:
        user = await self._get_user(request, db_session)
        form = Form(ReplyForm, request)

        if await form.validate():
            reply_tweet = await tweet_service.create_tweet(
                db_session, user.id, form.data.content, parent_id=tweet_id
            )
            flash_success(request, "Reply posted!")
            if reply_tweet.parent and str(reply_tweet.parent.user_id) != str(user.id):
                await notify_user(
                    str(reply_tweet.parent.user_id),
                    "generic",
                    title=f"{user.name} replied to your tweet",
                    message=form.data.content[:100],
                )
        else:
            errors = "; ".join(form.errors.values())
            flash_error(request, f"Could not post reply: {errors}")

        return Redirect(path=f"/tweet/{tweet_id}")

    @post("/{tweet_id:uuid}/like", guards=[auth_guard])
    async def like(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> Redirect:
        referer = request.headers.get("referer", "/")
        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path=referer)
        user = await self._get_user(request, db_session)
        liked = await like_service.toggle_like(db_session, user.id, tweet_id)
        flash_success(request, "Liked!" if liked else "Unliked")
        if liked:
            tweet = await tweet_service.get_tweet_by_id(db_session, tweet_id)
            if tweet and str(tweet.user_id) != str(user.id):
                await notify_user(
                    str(tweet.user_id),
                    "generic",
                    title=f"{user.name} liked your tweet",
                    message=tweet.content[:100],
                )
        return Redirect(path=referer)

    @post("/{tweet_id:uuid}/retweet", guards=[auth_guard])
    async def retweet(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> Redirect:
        referer = request.headers.get("referer", "/")
        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path=referer)
        user = await self._get_user(request, db_session)
        result = await tweet_service.create_retweet(db_session, user.id, tweet_id)
        if result:
            flash_success(request, "Retweeted!")
            if result.retweet_of and str(result.retweet_of.user_id) != str(user.id):
                await notify_user(
                    str(result.retweet_of.user_id),
                    "generic",
                    title=f"{user.name} retweeted your tweet",
                    message=result.retweet_of.content[:100],
                )
        else:
            flash_error(request, "Already retweeted or tweet not found")
        return Redirect(path=referer)

    @post("/{tweet_id:uuid}/bookmark", guards=[auth_guard])
    async def bookmark(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> Redirect:
        referer = request.headers.get("referer", "/")
        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path=referer)
        user = await self._get_user(request, db_session)
        saved = await feed_service.toggle_bookmark(db_session, user.id, tweet_id)
        flash_success(request, "Bookmarked!" if saved else "Bookmark removed")
        return Redirect(path=referer)

    @post("/{tweet_id:uuid}/delete", guards=[auth_guard])
    async def delete(self, request: Request, db_session: AsyncSession, tweet_id: UUID) -> Redirect:
        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path="/")
        user = await self._get_user(request, db_session)
        is_owner = await tweet_service.check_tweet_ownership(db_session, tweet_id, user.id)
        if not is_owner:
            flash_error(request, "You can only delete your own tweets")
            return Redirect(path="/")

        await tweet_service.soft_delete_tweet(db_session, tweet_id)
        flash_success(request, "Tweet deleted")
        return Redirect(path="/")
