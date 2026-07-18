"""Remembered OAuth2 consent grant model.

Records that a user has approved a set of scopes for an OAuth2 client so the
``/oauth/authorize`` consent screen can be skipped on a later authorization
request that stays within the already-granted scopes. A request for scopes
beyond the grant re-prompts, and approval widens the stored grant.
"""

from datetime import datetime
from uuid import UUID

from advanced_alchemy.types import DateTimeUTC
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class ConsentGrant(Base):
    """A user's remembered consent for an OAuth2 client's scopes."""

    __tablename__ = "oauth2_consent_grants"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_id", name="uq_oauth2_consent_grants_user_client"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    scopes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTimeUTC(timezone=True), nullable=False)

    @property
    def scope_list(self) -> list[str]:
        """Parse newline-delimited scopes into a list."""
        return [scope.strip() for scope in self.scopes.split("\n") if scope.strip()]
