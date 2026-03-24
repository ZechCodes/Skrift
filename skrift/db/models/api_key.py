"""API key model for programmatic authentication."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base

if TYPE_CHECKING:
    from skrift.db.models.user import User


class APIKey(Base):
    """A database-backed API key for programmatic authentication.

    Keys are stored as SHA-256 hashes. The raw key (``sk_...``) is only
    available at creation time. Each key can optionally scope its effective
    permissions to a subset of the owning user's permissions/roles.
    """

    __tablename__ = "api_keys"

    # Owner
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Display metadata
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Key storage (prefix for display, hash for lookup)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)

    # Permission scoping (newline-delimited, nullable = inherit all user permissions)
    scoped_permissions: Mapped[str | None] = mapped_column(Text, nullable=True)
    scoped_roles: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Usage tracking
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Refresh token (for key rotation)
    refresh_token_hash: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="api_keys")

    @property
    def scoped_permission_list(self) -> list[str]:
        """Parse newline-delimited scoped_permissions into a list."""
        if not self.scoped_permissions:
            return []
        return [p.strip() for p in self.scoped_permissions.split("\n") if p.strip()]

    @property
    def scoped_role_list(self) -> list[str]:
        """Parse newline-delimited scoped_roles into a list."""
        if not self.scoped_roles:
            return []
        return [r.strip() for r in self.scoped_roles.split("\n") if r.strip()]

    @property
    def is_expired(self) -> bool:
        """Check if the key has expired."""
        if self.expires_at is None:
            return False
        from datetime import timezone

        return datetime.now(tz=timezone.utc) >= self.expires_at

    @property
    def refresh_token_expired(self) -> bool:
        """Check if the refresh token has expired."""
        if self.refresh_token_expires_at is None:
            return True  # No refresh token = considered expired
        from datetime import timezone

        return datetime.now(tz=timezone.utc) >= self.refresh_token_expires_at
