"""OAuth2 client model for the authorization server."""

from sqlalchemy import String, Text, Boolean
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

    @property
    def redirect_uri_list(self) -> list[str]:
        """Parse newline-delimited redirect_uris into a list."""
        return [uri.strip() for uri in self.redirect_uris.split("\n") if uri.strip()]

    @property
    def allowed_scope_list(self) -> list[str]:
        """Parse newline-delimited allowed_scopes into a list."""
        return [s.strip() for s in self.allowed_scopes.split("\n") if s.strip()]
