"""SQLAlchemy models for persistent worker backends."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class WorkerStateRecord(Base):
    """Persistent worker key/value state."""

    __tablename__ = "worker_state"

    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSON, nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_worker_state_key", "key"),
        Index("ix_worker_state_expires_at", "expires_at"),
    )


class WorkerEventRecord(Base):
    """Append-only worker event log row."""

    __tablename__ = "worker_events"

    stream: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("stream", "position", name="uq_worker_events_stream_position"),
        Index("ix_worker_events_stream_position", "stream", "position"),
        Index("ix_worker_events_stream_job_id_position", "stream", "job_id", "position"),
    )


class WorkerQueueRecord(Base):
    """Persistent worker queue entry."""

    __tablename__ = "worker_queue"

    job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    queue: Mapped[str] = mapped_column(String(255), nullable=False)
    job: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dead_lettered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_worker_queue_queue_visible", "queue", "visible_at"),
        Index("ix_worker_queue_claim_expires_at", "claim_expires_at"),
        Index("ix_worker_queue_dead_lettered", "dead_lettered"),
        Index("ix_worker_queue_job_id", "job_id"),
    )


class WorkerDeadLetterRecord(Base):
    """Persistent worker dead-letter entry."""

    __tablename__ = "worker_dead_letters"

    entry_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    queue: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[str] = mapped_column(String(255), nullable=False)
    cause: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    exception_types: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entry: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    entry_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_worker_dlq_entry_id", "entry_id"),
        Index("ix_worker_dlq_queue", "queue"),
        Index("ix_worker_dlq_job_type", "job_type"),
        Index("ix_worker_dlq_cause", "cause"),
        Index("ix_worker_dlq_state", "state"),
        Index("ix_worker_dlq_created_at", "entry_created_at"),
    )


class WorkerArchiveEventRecord(Base):
    """Cold-storage copy of a worker event."""

    __tablename__ = "worker_archive_events"

    stream: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("stream", "position", name="uq_worker_archive_events_stream_position"),
        Index("ix_worker_archive_events_stream_position", "stream", "position"),
    )


class WorkerArchiveSnapshotRecord(Base):
    """Cold-storage state snapshot."""

    __tablename__ = "worker_archive_snapshots"

    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSON, nullable=True
    )
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_worker_archive_snapshots_key_time", "key", "snapshot_at"),
    )
