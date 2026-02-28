"""Add featured_asset_id to pages.

Revision ID: m7n8o9p0q1r2
Revises: a1b2c3d4e5f6
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa

revision = "m7n8o9p0q1r2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("pages") as batch_op:
        batch_op.add_column(
            sa.Column("featured_asset_id", sa.Uuid(), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_pages_featured_asset_id",
            "assets",
            ["featured_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("pages") as batch_op:
        batch_op.drop_constraint("fk_pages_featured_asset_id", type_="foreignkey")
        batch_op.drop_column("featured_asset_id")
