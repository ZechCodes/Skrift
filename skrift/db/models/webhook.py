"""Database models for durable outbound webhooks."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from skrift.db.base import Base


class WebhookDelivery(Base):
    """Durable outbox row for one outbound webhook delivery."""

    __tablename__ = "webhook_deliveries"

    profile: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    attempts: Mapped[list["WebhookDeliveryAttempt"]] = relationship(
        "WebhookDeliveryAttempt",
        back_populates="delivery",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("profile", "idempotency_key", name="uq_webhook_delivery_idempotency"),
        Index("ix_webhook_deliveries_profile_status", "profile", "status"),
        Index("ix_webhook_deliveries_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_locked_until", "locked_until"),
        Index("ix_webhook_deliveries_retention_until", "retention_until"),
    )


class WebhookDeliveryAttempt(Base):
    """Append-only forensic record for one outbound webhook HTTP attempt."""

    __tablename__ = "webhook_delivery_attempts"

    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("webhook_deliveries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)

    delivery: Mapped[WebhookDelivery] = relationship(
        "WebhookDelivery",
        back_populates="attempts",
    )

    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_webhook_delivery_attempt_number",
        ),
        Index("ix_webhook_attempts_delivery_attempt", "delivery_id", "attempt_number"),
    )
