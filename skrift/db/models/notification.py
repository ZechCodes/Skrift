from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class StoredNotification(Base):
    """Persistent notification storage for cross-replica backends."""

    __tablename__ = "stored_notifications"

    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    group_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.now
    )

    __table_args__ = (
        Index("ix_stored_notifications_scope_scope_id", "scope", "scope_id"),
        Index("ix_stored_notifications_scope_scope_id_group", "scope", "scope_id", "group_key"),
        Index("ix_stored_notifications_notified_at", "notified_at"),
    )
