"""Add dismissed_notifications table for per-subscriber dismissals.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dismissed_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscriber_key", sa.String(255), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscriber_key", "notification_id",
            name="uq_dismissed_subscriber_notification",
        ),
    )
    op.create_index(
        "ix_dismissed_notifications_subscriber_key",
        "dismissed_notifications",
        ["subscriber_key"],
    )
    op.create_index(
        "ix_dismissed_notifications_notification_id",
        "dismissed_notifications",
        ["notification_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dismissed_notifications_notification_id", table_name="dismissed_notifications")
    op.drop_index("ix_dismissed_notifications_subscriber_key", table_name="dismissed_notifications")
    op.drop_table("dismissed_notifications")
