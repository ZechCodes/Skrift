from uuid import UUID

from sqlalchemy import Text, Boolean, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base


class Tweet(Base):
    __tablename__ = "tweets"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], lazy="selectin")

    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Self-referential FK for replies
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tweets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent: Mapped["Tweet | None"] = relationship(
        "Tweet", remote_side="Tweet.id", foreign_keys=[parent_id], lazy="selectin"
    )

    # Self-referential FK for retweets
    retweet_of_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tweets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retweet_of: Mapped["Tweet | None"] = relationship(
        "Tweet", remote_side="Tweet.id", foreign_keys=[retweet_of_id], lazy="selectin"
    )

    # Denormalized counters
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reply_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retweet_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
