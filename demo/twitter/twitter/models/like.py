from uuid import UUID

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class Like(Base):
    __tablename__ = "tweet_likes"
    __table_args__ = (
        UniqueConstraint("user_id", "tweet_id", name="uq_tweet_likes_user_tweet"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tweet_id: Mapped[UUID] = mapped_column(ForeignKey("tweets.id", ondelete="CASCADE"), nullable=False, index=True)
