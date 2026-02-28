"""Add assets table for file/media storage.

Revision ID: g9h0i1j2k3l4
Revises: f8a9b0c1d2e3
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa

revision = "g9h0i1j2k3l4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(1024), nullable=False),
        sa.Column("store", sa.String(64), nullable=False, server_default="default"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(512), nullable=False, server_default=""),
        sa.Column("alt_text", sa.String(500), nullable=False, server_default=""),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_assets_key", "assets", ["key"])
    op.create_index("ix_assets_store", "assets", ["store"])
    op.create_index("ix_assets_content_hash", "assets", ["content_hash"])
    op.create_index("ix_assets_folder", "assets", ["folder"])
    op.create_index("ix_assets_user_id", "assets", ["user_id"])
    op.create_index("ix_asset_store_content_hash", "assets", ["store", "content_hash"])


def downgrade() -> None:
    op.drop_index("ix_asset_store_content_hash", table_name="assets")
    op.drop_index("ix_assets_user_id", table_name="assets")
    op.drop_index("ix_assets_folder", table_name="assets")
    op.drop_index("ix_assets_content_hash", table_name="assets")
    op.drop_index("ix_assets_store", table_name="assets")
    op.drop_index("ix_assets_key", table_name="assets")
    op.drop_table("assets")
