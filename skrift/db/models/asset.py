"""Asset model for file/media storage."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class Asset(Base):
    """A stored file or media asset."""

    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_asset_store_content_hash", "store", "content_hash"),
    )

    key: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    store: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    folder: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    alt_text: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
