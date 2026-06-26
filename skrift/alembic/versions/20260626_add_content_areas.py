"""Add content_areas table for code-declared editable content.

Revision ID: b0c1d2e3f4a5
Revises: f2a3b4c5d6e7
Create Date: 2026-06-26
"""

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import DateTimeUTC, GUID


revision = "b0c1d2e3f4a5"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_areas",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_content_areas_key", "content_areas", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_content_areas_key", table_name="content_areas")
    op.drop_table("content_areas")
