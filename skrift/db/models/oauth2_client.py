"""OAuth2 client model for the authorization server."""

from datetime import datetime

from sqlalchemy import String, Text, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class OAuth2Client(Base):
    """A registered OAuth2 client application."""

    __tablename__ = "oauth2_clients"

    client_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    client_secret: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uris: Mapped[str] = mapped_column(Text, default="", nullable=False)
    allowed_scopes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Dynamic Client Registration (RFC 7591) metadata. Confidential clients
    # created through the admin UI leave these at their defaults; clients
    # created via POST /oauth/register are public and flagged here.
    is_dynamically_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(64), default="client_secret_post", nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_by_ip: Mapped[str | None] = mapped_column(String(45), index=True, nullable=True)
    client_id_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def redirect_uri_list(self) -> list[str]:
        """Parse newline-delimited redirect_uris into a list."""
        return [uri.strip() for uri in self.redirect_uris.split("\n") if uri.strip()]

    @property
    def allowed_scope_list(self) -> list[str]:
        """Parse newline-delimited allowed_scopes into a list."""
        return [s.strip() for s in self.allowed_scopes.split("\n") if s.strip()]
