"""Add republish metadata and API key constraints.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import DateTimeUTC, GUID


revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("constraints", sa.Text(), nullable=True))

    op.create_table(
        "page_republishes",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("page_id", GUID(length=16), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("source_origin", sa.String(length=1024), nullable=False),
        sa.Column("source_updated_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("image_url", sa.String(length=2048), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_url", name="uq_page_republishes_canonical_url"),
    )
    op.create_index("ix_page_republishes_page_id", "page_republishes", ["page_id"], unique=True)
    op.create_index("ix_page_republishes_source_origin", "page_republishes", ["source_origin"])

    op.create_table(
        "republish_site_links",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("user_id", GUID(length=16), nullable=False),
        sa.Column("site_name", sa.String(length=255), nullable=True),
        sa.Column("site_url", sa.String(length=2048), nullable=False),
        sa.Column("target_origin", sa.String(length=1024), nullable=False),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "target_origin", name="uq_republish_site_links_user_target"),
    )
    op.create_index("ix_republish_site_links_user_id", "republish_site_links", ["user_id"])
    op.create_index("ix_republish_site_links_target_origin", "republish_site_links", ["target_origin"])


def downgrade() -> None:
    op.drop_index("ix_republish_site_links_target_origin", table_name="republish_site_links")
    op.drop_index("ix_republish_site_links_user_id", table_name="republish_site_links")
    op.drop_table("republish_site_links")

    op.drop_index("ix_page_republishes_source_origin", table_name="page_republishes")
    op.drop_index("ix_page_republishes_page_id", table_name="page_republishes")
    op.drop_table("page_republishes")

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("constraints")
