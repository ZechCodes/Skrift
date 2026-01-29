"""OAuth account model for storing multiple OAuth identities per user."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.user import User


class OAuthAccount(Base):
    """OAuth account model linking OAuth provider identities to users.

    This allows a single user to have multiple OAuth provider accounts
    linked to their profile, enabling login via different providers.
    """

    __tablename__ = "oauth_accounts"

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="oauth_accounts")

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_id", name="uq_oauth_provider_account"
        ),
    )
