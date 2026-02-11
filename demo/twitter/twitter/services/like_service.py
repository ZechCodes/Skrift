from uuid import UUID

from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.lib.hooks import hooks
from twitter.hooks import AFTER_TWEET_LIKE, AFTER_TWEET_UNLIKE
from twitter.models.like import Like
from twitter.models.tweet import Tweet


async def toggle_like(db_session: AsyncSession, user_id: UUID, tweet_id: UUID) -> bool:
    """Toggle like on a tweet. Returns True if liked, False if unliked."""
    existing = await db_session.execute(
        select(Like).where(and_(Like.user_id == user_id, Like.tweet_id == tweet_id))
    )
    like = existing.scalar_one_or_none()

    tweet = await db_session.execute(select(Tweet).where(Tweet.id == tweet_id))
    tweet_obj = tweet.scalar_one_or_none()
    if not tweet_obj:
        return False

    if like:
        await db_session.execute(
            delete(Like).where(and_(Like.user_id == user_id, Like.tweet_id == tweet_id))
        )
        tweet_obj.like_count = max(0, tweet_obj.like_count - 1)
        await db_session.commit()
        await hooks.do_action(AFTER_TWEET_UNLIKE, user_id, tweet_id)
        return False
    else:
        db_session.add(Like(user_id=user_id, tweet_id=tweet_id))
        tweet_obj.like_count += 1
        await db_session.commit()
        await hooks.do_action(AFTER_TWEET_LIKE, user_id, tweet_id)
        return True


async def has_user_liked(db_session: AsyncSession, user_id: UUID, tweet_id: UUID) -> bool:
    result = await db_session.execute(
        select(Like).where(and_(Like.user_id == user_id, Like.tweet_id == tweet_id))
    )
    return result.scalar_one_or_none() is not None


async def get_liked_tweet_ids(db_session: AsyncSession, user_id: UUID, tweet_ids: list[UUID]) -> set[UUID]:
    if not tweet_ids:
        return set()
    result = await db_session.execute(
        select(Like.tweet_id).where(
            and_(Like.user_id == user_id, Like.tweet_id.in_(tweet_ids))
        )
    )
    return set(result.scalars().all())
