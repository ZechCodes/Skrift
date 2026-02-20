"""Add source_key column and notification_subscriptions table.

Adds source_key to stored_notifications (populated from scope || ':' || scope_id),
and creates the notification_subscriptions table for persistent subscription edges.

Revision ID: e7f8a9b0c1d2
Revises: d6e4f7a8b9c2
Create Date: 2026-02-20
"""

from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d6e4f7a8b9c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add source_key column to stored_notifications
    op.add_column(
        "stored_notifications",
        sa.Column("source_key", sa.String(255), nullable=False, server_default=""),
    )

    # 2. Populate source_key from existing scope/scope_id
    op.execute(
        "UPDATE stored_notifications SET source_key = scope || ':' || scope_id"
    )

    # 3. Add indexes on source_key
    op.create_index(
        "ix_stored_notifications_source_key",
        "stored_notifications",
        ["source_key"],
    )
    op.create_index(
        "ix_stored_notifications_source_key_group",
        "stored_notifications",
        ["source_key", "group_key"],
    )
    op.create_index(
        "ix_stored_notifications_source_key_dmode_notified",
        "stored_notifications",
        ["source_key", "delivery_mode", "notified_at"],
    )

    # 4. Create notification_subscriptions table
    op.create_table(
        "notification_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscriber_key", sa.String(255), nullable=False),
        sa.Column("source_key", sa.String(255), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscriber_key", "source_key",
            name="uq_notification_sub_subscriber_source",
        ),
    )
    op.create_index(
        "ix_notification_subscriptions_subscriber_key",
        "notification_subscriptions",
        ["subscriber_key"],
    )
    op.create_index(
        "ix_notification_subscriptions_source_key",
        "notification_subscriptions",
        ["source_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_subscriptions_source_key", table_name="notification_subscriptions")
    op.drop_index("ix_notification_subscriptions_subscriber_key", table_name="notification_subscriptions")
    op.drop_table("notification_subscriptions")

    op.drop_index("ix_stored_notifications_source_key_dmode_notified", table_name="stored_notifications")
    op.drop_index("ix_stored_notifications_source_key_group", table_name="stored_notifications")
    op.drop_index("ix_stored_notifications_source_key", table_name="stored_notifications")
    op.drop_column("stored_notifications", "source_key")
