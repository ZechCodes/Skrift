"""OAuth account model for storing multiple OAuth identities per user."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.user import User


class OAuthAccount(Base):
    """OAuth account model linking OAuth provider identities to users.

    This allows a single user to have multiple OAuth provider accounts
    linked to their profile, enabling login via different providers.

    The provider_metadata column stores the full raw OAuth provider response,
    which varies by provider:

    - Discord: id, username, global_name, discriminator, avatar, email, verified, locale
    - GitHub: id, login, name, email, avatar_url, bio, company, location, public_repos
    - Google: id, email, name, picture, verified_email, locale, hd
    - Twitter: id, username, name
    - Microsoft: id, displayName, mail, userPrincipalName
    - Facebook: id, name, email, picture.data.url
    """

    __tablename__ = "oauth_accounts"

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, default=None
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="oauth_accounts")

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_id", name="uq_oauth_provider_account"
        ),
    )
