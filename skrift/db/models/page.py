from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base
from skrift.db.models.page_asset import page_assets

if TYPE_CHECKING:
    from skrift.db.models.asset import Asset
    from skrift.db.models.page_revision import PageRevision


class Page(Base):
    """Page model for content management."""

    __tablename__ = "pages"

    # Author relationship (optional - pages may not have an author)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    user: Mapped["User"] = relationship("User", back_populates="pages")

    # Content fields
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    type: Mapped[str] = mapped_column(String(50), nullable=False, default="page", server_default="page", index=True)

    # Publication fields
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Scheduling field
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Ordering field
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)

    # SEO fields
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    og_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    og_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    og_image: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meta_robots: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Revisions relationship (defined after PageRevision exists)
    revisions: Mapped[list["PageRevision"]] = relationship(
        "PageRevision",
        back_populates="page",
        cascade="all, delete-orphan",
        order_by="desc(PageRevision.revision_number)",
    )

    # Featured asset (cover image)
    featured_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    featured_asset: Mapped["Asset | None"] = relationship(
        "Asset", lazy="selectin", foreign_keys=[featured_asset_id]
    )

    # Assets relationship (many-to-many via page_assets)
    assets: Mapped[list["Asset"]] = relationship(
        "Asset", secondary=page_assets, lazy="selectin"
    )
