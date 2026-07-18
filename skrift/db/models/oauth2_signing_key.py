"""OAuth2 signing key model for asymmetric access-token signing."""

from datetime import datetime

from advanced_alchemy.types import DateTimeUTC
from sqlalchemy import String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class OAuth2SigningKey(Base):
    """An asymmetric key pair used to sign OAuth2 access-token JWTs.

    The private key is stored PEM-encoded. Third parties verify tokens
    against the matching public key published at the JWKS endpoint, keyed
    by ``kid``. Exactly one key is active at a time; retired keys stay
    published until they age out so tokens they signed remain verifiable.
    """

    __tablename__ = "oauth2_signing_keys"

    kid: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    private_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(16), default="ES256", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTimeUTC(timezone=True), nullable=True
    )
