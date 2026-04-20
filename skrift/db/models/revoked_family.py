"""Revoked refresh-token family model.

Used to mass-revoke a refresh-token family when we detect that a
previously-rotated (and therefore revoked) refresh token has been
presented again — the classic reuse-detection signal from RFC 6749 §10.4.

A family is an opaque hex identifier (uuid4) stamped into every refresh
token that descends from the same authorization code. When reuse is
detected, we add the family id here; subsequent refresh attempts for
any token in the family fail fast without issuing new tokens.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class RevokedFamily(Base):
    """A revoked refresh-token family."""

    __tablename__ = "revoked_token_families"

    family_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
