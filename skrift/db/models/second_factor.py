"""Second-factor enrollment records."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.user import User


class SecondFactorEnrollment(Base):
    """A user's enrolled second-factor credential or device."""

    __tablename__ = "second_factor_enrollments"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    factor_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    factor_type: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transports: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrollment_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped["User"] = relationship("User", back_populates="second_factor_enrollments")

    __table_args__ = (
        UniqueConstraint(
            "factor_key", "credential_id", name="uq_second_factor_enrollment_credential"
        ),
    )

    @property
    def transport_list(self) -> list[str]:
        """Parse newline-delimited transports into a list."""
        if not self.transports:
            return []
        return [transport.strip() for transport in self.transports.split("\n") if transport.strip()]
