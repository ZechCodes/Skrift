"""Add text_pattern_ops index for worker_state key prefix scans.

Revision ID: 9f8e7d6c5b4a
Revises: b0c1d2e3f4a5
Create Date: 2026-07-10
"""

from alembic import op


revision = "9f8e7d6c5b4a"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_worker_state_key_pattern",
        "worker_state",
        ["key"],
        unique=False,
        postgresql_ops={"key": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_worker_state_key_pattern", table_name="worker_state")
