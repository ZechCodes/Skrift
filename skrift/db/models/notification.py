from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class StoredNotification(Base):
    """Persistent notification storage for cross-replica backends."""

    __tablename__ = "stored_notifications"

    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    group_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.now
    )

    __table_args__ = (
        Index("ix_stored_notifications_scope_scope_id", "scope", "scope_id"),
        Index("ix_stored_notifications_scope_scope_id_group", "scope", "scope_id", "group_key"),
        Index("ix_stored_notifications_notified_at", "notified_at"),
        Index("ix_stored_notifications_scope_sid_dmode_notified", "scope", "scope_id", "delivery_mode", "notified_at"),
        Index("ix_stored_notifications_source_key", "source_key"),
        Index("ix_stored_notifications_source_key_group", "source_key", "group_key"),
        Index("ix_stored_notifications_source_key_dmode_notified", "source_key", "delivery_mode", "notified_at"),
    )


class NotificationSubscription(Base):
    """Persistent subscription edges for the notification source graph.

    Maps a subscriber (e.g. ``user:alice``) to a source (e.g. ``blog:tech``).
    Ephemeral edges (session→user, session→global) live only in the in-memory
    SourceRegistry and are NOT stored here.
    """

    __tablename__ = "notification_subscriptions"

    subscriber_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint("subscriber_key", "source_key", name="uq_notification_sub_subscriber_source"),
        Index("ix_notification_subscriptions_subscriber_key", "subscriber_key"),
        Index("ix_notification_subscriptions_source_key", "source_key"),
    )


class DismissedNotification(Base):
    __tablename__ = "dismissed_notifications"

    subscriber_key: Mapped[str] = mapped_column(String(255), nullable=False)
    notification_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.now
    )

    __table_args__ = (
        UniqueConstraint(
            "subscriber_key", "notification_id",
            name="uq_dismissed_subscriber_notification",
        ),
        Index("ix_dismissed_notifications_subscriber_key", "subscriber_key"),
        Index("ix_dismissed_notifications_notification_id", "notification_id"),
    )
