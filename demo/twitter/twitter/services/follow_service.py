from uuid import UUID

from sqlalchemy import select, and_, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.db.models.user import User
from skrift.lib.hooks import hooks
from twitter.hooks import AFTER_USER_FOLLOW, AFTER_USER_UNFOLLOW
from twitter.models.follow import Follow


async def toggle_follow(db_session: AsyncSession, follower_id: UUID, following_id: UUID) -> bool:
    """Toggle follow. Returns True if now following, False if unfollowed."""
    if follower_id == following_id:
        return False

    existing = await db_session.execute(
        select(Follow).where(
            and_(Follow.follower_id == follower_id, Follow.following_id == following_id)
        )
    )
    follow = existing.scalar_one_or_none()

    if follow:
        await db_session.execute(
            delete(Follow).where(
                and_(Follow.follower_id == follower_id, Follow.following_id == following_id)
            )
        )
        await db_session.commit()
        await hooks.do_action(AFTER_USER_UNFOLLOW, follower_id, following_id)
        return False
    else:
        db_session.add(Follow(follower_id=follower_id, following_id=following_id))
        await db_session.commit()
        await hooks.do_action(AFTER_USER_FOLLOW, follower_id, following_id)
        return True


async def is_following(db_session: AsyncSession, follower_id: UUID, following_id: UUID) -> bool:
    result = await db_session.execute(
        select(Follow).where(
            and_(Follow.follower_id == follower_id, Follow.following_id == following_id)
        )
    )
    return result.scalar_one_or_none() is not None


async def get_following_ids(db_session: AsyncSession, user_id: UUID) -> set[UUID]:
    result = await db_session.execute(
        select(Follow.following_id).where(Follow.follower_id == user_id)
    )
    return set(result.scalars().all())


async def get_follower_count(db_session: AsyncSession, user_id: UUID) -> int:
    result = await db_session.execute(
        select(func.count()).select_from(Follow).where(Follow.following_id == user_id)
    )
    return result.scalar() or 0


async def get_following_count(db_session: AsyncSession, user_id: UUID) -> int:
    result = await db_session.execute(
        select(func.count()).select_from(Follow).where(Follow.follower_id == user_id)
    )
    return result.scalar() or 0


async def get_followers(
    db_session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[User]:
    result = await db_session.execute(
        select(User)
        .join(Follow, Follow.follower_id == User.id)
        .where(Follow.following_id == user_id)
        .order_by(Follow.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_following(
    db_session: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> list[User]:
    result = await db_session.execute(
        select(User)
        .join(Follow, Follow.following_id == User.id)
        .where(Follow.follower_id == user_id)
        .order_by(Follow.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())
