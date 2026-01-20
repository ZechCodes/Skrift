from datetime import datetime

from sqlalchemy import String, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from basesite.db.base import Base


class User(Base):
    """User model for OAuth authentication."""

    __tablename__ = "users"

    # OAuth identifiers
    oauth_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    oauth_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Profile data from OAuth provider
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    picture_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Application fields
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
