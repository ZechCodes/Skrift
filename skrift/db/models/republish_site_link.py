"""Republish destination links for a user account."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.user import User


class RepublishSiteLink(Base):
    """A destination site this account can publish to."""

    __tablename__ = "republish_site_links"
    __table_args__ = (
        UniqueConstraint("user_id", "target_origin", name="uq_republish_site_links_user_target"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    site_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    site_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    target_origin: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)

    user: Mapped["User"] = relationship("User")
