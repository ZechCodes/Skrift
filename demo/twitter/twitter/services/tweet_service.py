from uuid import UUID

from markupsafe import Markup
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.lib.hooks import hooks
from twitter.hooks import (
    BEFORE_TWEET_SAVE, AFTER_TWEET_SAVE,
    BEFORE_TWEET_DELETE, AFTER_TWEET_DELETE,
    TWEET_CONTENT_RENDER,
)
from twitter.models.tweet import Tweet


async def create_tweet(
    db_session: AsyncSession,
    user_id: UUID,
    content: str,
    parent_id: UUID | None = None,
) -> Tweet:
    tweet = Tweet(
        user_id=user_id,
        content=content,
        parent_id=parent_id,
    )

    await hooks.do_action(BEFORE_TWEET_SAVE, tweet, is_new=True)

    db_session.add(tweet)

    # Increment parent reply count
    if parent_id:
        parent = await get_tweet_by_id(db_session, parent_id)
        if parent:
            parent.reply_count += 1

    await db_session.commit()
    await db_session.refresh(tweet)

    await hooks.do_action(AFTER_TWEET_SAVE, tweet, is_new=True)
    return tweet


async def create_retweet(
    db_session: AsyncSession,
    user_id: UUID,
    retweet_of_id: UUID,
) -> Tweet | None:
    original = await get_tweet_by_id(db_session, retweet_of_id)
    if not original:
        return None

    # Check if already retweeted
    existing = await db_session.execute(
        select(Tweet).where(
            and_(
                Tweet.user_id == user_id,
                Tweet.retweet_of_id == retweet_of_id,
                Tweet.is_deleted == False,
            )
        )
    )
    if existing.scalar_one_or_none():
        return None

    tweet = Tweet(
        user_id=user_id,
        content=original.content,
        retweet_of_id=retweet_of_id,
    )

    await hooks.do_action(BEFORE_TWEET_SAVE, tweet, is_new=True)

    db_session.add(tweet)
    original.retweet_count += 1

    await db_session.commit()
    await db_session.refresh(tweet)

    await hooks.do_action(AFTER_TWEET_SAVE, tweet, is_new=True)
    return tweet


async def get_tweet_by_id(db_session: AsyncSession, tweet_id: UUID) -> Tweet | None:
    result = await db_session.execute(
        select(Tweet).where(and_(Tweet.id == tweet_id, Tweet.is_deleted == False))
    )
    return result.scalar_one_or_none()


async def get_tweet_replies(
    db_session: AsyncSession,
    tweet_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    result = await db_session.execute(
        select(Tweet)
        .where(and_(Tweet.parent_id == tweet_id, Tweet.is_deleted == False))
        .order_by(Tweet.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_user_tweets(
    db_session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    result = await db_session.execute(
        select(Tweet)
        .where(
            and_(
                Tweet.user_id == user_id,
                Tweet.is_deleted == False,
                Tweet.parent_id.is_(None),
            )
        )
        .order_by(Tweet.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def soft_delete_tweet(db_session: AsyncSession, tweet_id: UUID) -> bool:
    tweet = await get_tweet_by_id(db_session, tweet_id)
    if not tweet:
        return False

    await hooks.do_action(BEFORE_TWEET_DELETE, tweet)

    tweet.is_deleted = True

    # Decrement parent reply count
    if tweet.parent_id:
        parent = await get_tweet_by_id(db_session, tweet.parent_id)
        if parent:
            parent.reply_count = max(0, parent.reply_count - 1)

    # Decrement original retweet count
    if tweet.retweet_of_id:
        original = await get_tweet_by_id(db_session, tweet.retweet_of_id)
        if original:
            original.retweet_count = max(0, original.retweet_count - 1)

    await db_session.commit()

    await hooks.do_action(AFTER_TWEET_DELETE, tweet)
    return True


async def check_tweet_ownership(
    db_session: AsyncSession, tweet_id: UUID, user_id: UUID
) -> bool:
    tweet = await get_tweet_by_id(db_session, tweet_id)
    if not tweet:
        return False
    return tweet.user_id == user_id


async def search_tweets(
    db_session: AsyncSession,
    query: str,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    result = await db_session.execute(
        select(Tweet)
        .where(
            and_(
                Tweet.content.ilike(f"%{query}%"),
                Tweet.is_deleted == False,
                Tweet.parent_id.is_(None),
            )
        )
        .order_by(Tweet.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def render_tweet_content(content: str) -> Markup:
    rendered = await hooks.apply_filters(TWEET_CONTENT_RENDER, content)
    return Markup(rendered)
