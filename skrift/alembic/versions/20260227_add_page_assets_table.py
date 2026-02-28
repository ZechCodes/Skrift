"""Add page_assets association table.

Revision ID: a1b2c3d4e5f6
Revises: g9h0i1j2k3l4
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "g9h0i1j2k3l4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_assets",
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("page_id", "asset_id"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("page_assets")
