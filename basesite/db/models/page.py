from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import String, Text, Boolean, DateTime, Index, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from basesite.db.base import Base


class PageType(str, Enum):
    """Page type enumeration."""

    POST = "post"
    PAGE = "page"


class Page(Base):
    """Page/Post model for content management."""

    __tablename__ = "pages"

    # Author relationship (optional - pages may not have an author)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    user: Mapped["User"] = relationship("User", back_populates="pages")

    # Content fields
    type: Mapped[PageType] = mapped_column(String(50), nullable=False, default=PageType.PAGE)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Publication fields
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_pages_type_published", "type", "is_published"),
    )
