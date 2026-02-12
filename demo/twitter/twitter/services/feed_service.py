from uuid import UUID

from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from twitter.models.tweet import Tweet
from twitter.models.bookmark import Bookmark
from twitter.services.follow_service import get_following_ids


def _tweet_load_options():
    """Eager-load options to avoid lazy loads in templates."""
    return [selectinload(Tweet.retweet_of).selectinload(Tweet.user)]


async def get_timeline(
    db_session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    """Get timeline: tweets from users the current user follows + own tweets."""
    following_ids = await get_following_ids(db_session, user_id)
    following_ids.add(user_id)

    result = await db_session.execute(
        select(Tweet)
        .options(*_tweet_load_options())
        .where(
            and_(
                Tweet.user_id.in_(following_ids),
                Tweet.is_deleted == False,
                Tweet.parent_id.is_(None),
            )
        )
        .order_by(Tweet.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_global_feed(
    db_session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    """Get all tweets in reverse chronological order."""
    result = await db_session.execute(
        select(Tweet)
        .options(*_tweet_load_options())
        .where(and_(Tweet.is_deleted == False, Tweet.parent_id.is_(None)))
        .order_by(Tweet.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_explore_feed(
    db_session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    """Get tweets sorted by engagement (likes + retweets + replies)."""
    result = await db_session.execute(
        select(Tweet)
        .options(*_tweet_load_options())
        .where(and_(Tweet.is_deleted == False, Tweet.parent_id.is_(None)))
        .order_by(
            (Tweet.like_count + Tweet.retweet_count + Tweet.reply_count).desc(),
            Tweet.created_at.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def toggle_bookmark(db_session: AsyncSession, user_id: UUID, tweet_id: UUID) -> bool:
    """Toggle bookmark. Returns True if bookmarked, False if removed."""
    existing = await db_session.execute(
        select(Bookmark).where(
            and_(Bookmark.user_id == user_id, Bookmark.tweet_id == tweet_id)
        )
    )
    bookmark = existing.scalar_one_or_none()

    if bookmark:
        await db_session.execute(
            delete(Bookmark).where(
                and_(Bookmark.user_id == user_id, Bookmark.tweet_id == tweet_id)
            )
        )
        await db_session.commit()
        return False
    else:
        db_session.add(Bookmark(user_id=user_id, tweet_id=tweet_id))
        await db_session.commit()
        return True


async def get_bookmarked_tweets(
    db_session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[Tweet]:
    result = await db_session.execute(
        select(Tweet)
        .options(*_tweet_load_options())
        .join(Bookmark, Bookmark.tweet_id == Tweet.id)
        .where(
            and_(Bookmark.user_id == user_id, Tweet.is_deleted == False)
        )
        .order_by(Bookmark.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_bookmarked_tweet_ids(
    db_session: AsyncSession, user_id: UUID, tweet_ids: list[UUID]
) -> set[UUID]:
    if not tweet_ids:
        return set()
    result = await db_session.execute(
        select(Bookmark.tweet_id).where(
            and_(Bookmark.user_id == user_id, Bookmark.tweet_id.in_(tweet_ids))
        )
    )
    return set(result.scalars().all())


async def get_retweeted_tweet_ids(
    db_session: AsyncSession, user_id: UUID, tweet_ids: list[UUID]
) -> set[UUID]:
    """Return which of the given tweet IDs the user has retweeted."""
    if not tweet_ids:
        return set()
    result = await db_session.execute(
        select(Tweet.retweet_of_id).where(
            and_(
                Tweet.user_id == user_id,
                Tweet.retweet_of_id.in_(tweet_ids),
                Tweet.is_deleted == False,
            )
        )
    )
    return set(result.scalars().all())
