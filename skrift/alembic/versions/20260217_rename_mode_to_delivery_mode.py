"""Rename mode column to delivery_mode on stored_notifications.

The 'mode' column name clashes with PostgreSQL's built-in mode()
ordered-set aggregate function, causing asyncpg.WrongObjectTypeError.

Revision ID: c5d3e8f9a0b1
Revises: b4c2d6e7f8a9
Create Date: 2026-02-17
"""

from alembic import op

revision = "c5d3e8f9a0b1"
down_revision = "b4c2d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_stored_notifications_scope_sid_mode_notified",
        table_name="stored_notifications",
    )
    op.alter_column(
        "stored_notifications",
        "mode",
        new_column_name="delivery_mode",
    )
    op.create_index(
        "ix_stored_notifications_scope_sid_dmode_notified",
        "stored_notifications",
        ["scope", "scope_id", "delivery_mode", "notified_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stored_notifications_scope_sid_dmode_notified",
        table_name="stored_notifications",
    )
    op.alter_column(
        "stored_notifications",
        "delivery_mode",
        new_column_name="mode",
    )
    op.create_index(
        "ix_stored_notifications_scope_sid_mode_notified",
        "stored_notifications",
        ["scope", "scope_id", "mode", "notified_at"],
    )
