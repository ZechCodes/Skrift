"""Page revision model for content versioning."""

from datetime import datetime, UTC
from uuid import UUID

from sqlalchemy import String, Text, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base


class PageRevision(Base):
    """Stores historical versions of page content."""

    __tablename__ = "page_revisions"

    # Relationship to page (cascade delete when page is deleted)
    page_id: Mapped[UUID] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page: Mapped["Page"] = relationship("Page", back_populates="revisions")

    # Who made the change (nullable for system changes)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user: Mapped["User"] = relationship("User")

    # Revision tracking
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Snapshot of page content at this revision
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
