"""Add persistent worker backend tables.

Revision ID: b8c9d0e1f2a3
Revises: f6a7b8c9d0e1
Create Date: 2026-05-05
"""

from alembic import op
import sqlalchemy as sa

revision = "b8c9d0e1f2a3"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_worker_state_key", "worker_state", ["key"])
    op.create_index("ix_worker_state_expires_at", "worker_state", ["expires_at"])

    op.create_table(
        "worker_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stream", "position", name="uq_worker_events_stream_position"),
    )
    op.create_index("ix_worker_events_stream_position", "worker_events", ["stream", "position"])

    op.create_table(
        "worker_queue",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("queue", sa.String(255), nullable=False),
        sa.Column("job", sa.JSON(), nullable=False),
        sa.Column("visible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_worker_queue_queue_visible", "worker_queue", ["queue", "visible_at"])
    op.create_index("ix_worker_queue_claim_expires_at", "worker_queue", ["claim_expires_at"])
    op.create_index("ix_worker_queue_dead_lettered", "worker_queue", ["dead_lettered"])
    op.create_index("ix_worker_queue_job_id", "worker_queue", ["job_id"])

    op.create_table(
        "worker_dead_letters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.String(64), nullable=False),
        sa.Column("queue", sa.String(255), nullable=False),
        sa.Column("job_type", sa.String(255), nullable=False),
        sa.Column("cause", sa.String(64), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("exception_types", sa.Text(), nullable=False),
        sa.Column("entry", sa.JSON(), nullable=False),
        sa.Column("entry_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id"),
    )
    op.create_index("ix_worker_dlq_entry_id", "worker_dead_letters", ["entry_id"])
    op.create_index("ix_worker_dlq_queue", "worker_dead_letters", ["queue"])
    op.create_index("ix_worker_dlq_job_type", "worker_dead_letters", ["job_type"])
    op.create_index("ix_worker_dlq_cause", "worker_dead_letters", ["cause"])
    op.create_index("ix_worker_dlq_state", "worker_dead_letters", ["state"])
    op.create_index("ix_worker_dlq_created_at", "worker_dead_letters", ["entry_created_at"])

    op.create_table(
        "worker_archive_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stream", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stream", "position", name="uq_worker_archive_events_stream_position"),
    )
    op.create_index(
        "ix_worker_archive_events_stream_position",
        "worker_archive_events",
        ["stream", "position"],
    )

    op.create_table(
        "worker_archive_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_archive_snapshots_key_time",
        "worker_archive_snapshots",
        ["key", "snapshot_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_archive_snapshots_key_time", table_name="worker_archive_snapshots")
    op.drop_table("worker_archive_snapshots")
    op.drop_index("ix_worker_archive_events_stream_position", table_name="worker_archive_events")
    op.drop_table("worker_archive_events")
    op.drop_index("ix_worker_dlq_created_at", table_name="worker_dead_letters")
    op.drop_index("ix_worker_dlq_state", table_name="worker_dead_letters")
    op.drop_index("ix_worker_dlq_cause", table_name="worker_dead_letters")
    op.drop_index("ix_worker_dlq_job_type", table_name="worker_dead_letters")
    op.drop_index("ix_worker_dlq_queue", table_name="worker_dead_letters")
    op.drop_index("ix_worker_dlq_entry_id", table_name="worker_dead_letters")
    op.drop_table("worker_dead_letters")
    op.drop_index("ix_worker_queue_job_id", table_name="worker_queue")
    op.drop_index("ix_worker_queue_dead_lettered", table_name="worker_queue")
    op.drop_index("ix_worker_queue_claim_expires_at", table_name="worker_queue")
    op.drop_index("ix_worker_queue_queue_visible", table_name="worker_queue")
    op.drop_table("worker_queue")
    op.drop_index("ix_worker_events_stream_position", table_name="worker_events")
    op.drop_table("worker_events")
    op.drop_index("ix_worker_state_expires_at", table_name="worker_state")
    op.drop_index("ix_worker_state_key", table_name="worker_state")
    op.drop_table("worker_state")
