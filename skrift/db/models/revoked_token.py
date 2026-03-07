"""Revoked token model for OAuth2 token revocation."""

from datetime import datetime

from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class RevokedToken(Base):
    """A revoked OAuth2 token tracked by its JTI."""

    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    token_type: Mapped[str] = mapped_column(String(20), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
