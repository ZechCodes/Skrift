"""Add sa_orm_sentinel column to stored_notifications.

The StoredNotification model inherits from UUIDAuditBase which includes
SentinelMixin, but the original migration omitted the sa_orm_sentinel
column. This fixes the mismatch so the ORM can use the table correctly.

Revision ID: d6e4f7a8b9c2
Revises: c5d3e8f9a0b1
Create Date: 2026-02-19
"""

from alembic import op
import sqlalchemy as sa

revision = "d6e4f7a8b9c2"
down_revision = "c5d3e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stored_notifications",
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stored_notifications", "sa_orm_sentinel")
