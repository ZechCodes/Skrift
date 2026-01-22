from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class Setting(Base):
    """Key-value setting storage for site configuration."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
