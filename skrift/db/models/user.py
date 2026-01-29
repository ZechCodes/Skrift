from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.oauth_account import OAuthAccount
    from skrift.db.models.page import Page
    from skrift.db.models.role import Role


class User(Base):
    """User model for OAuth authentication."""

    __tablename__ = "users"

    # Profile data from OAuth provider
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    picture_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Application fields
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(
        "OAuthAccount", back_populates="user", cascade="all, delete-orphan"
    )
    pages: Mapped[list["Page"]] = relationship("Page", back_populates="user")
    roles: Mapped[list["Role"]] = relationship(
        "Role", secondary="user_roles", back_populates="users", lazy="selectin"
    )
