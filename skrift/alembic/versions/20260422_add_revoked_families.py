"""Add revoked_token_families table for refresh-token reuse detection.

Records families (groups of rotated refresh tokens) that have been
invalidated because someone presented an already-consumed refresh token
from that family — the RFC 6749 §10.4 compromise-indicator signal.

Revision ID: f6a7b8c9d0e1
Revises: e5f6g7h8i9j0
Create Date: 2026-04-22
"""

from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6g7h8i9j0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revoked_token_families",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_revoked_token_families_family_id",
        "revoked_token_families",
        ["family_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_revoked_token_families_family_id", table_name="revoked_token_families")
    op.drop_table("revoked_token_families")
