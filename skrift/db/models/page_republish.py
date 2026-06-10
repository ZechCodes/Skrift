"""Republished page metadata."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from advanced_alchemy.types import DateTimeUTC
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.page import Page


class PageRepublish(Base):
    """Metadata linking a local page to a canonical remote source."""

    __tablename__ = "page_republishes"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_page_republishes_canonical_url"),
    )

    page_id: Mapped[UUID] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_origin: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTimeUTC(timezone=True),
        nullable=True,
    )
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    page: Mapped["Page"] = relationship("Page", back_populates="republish")
