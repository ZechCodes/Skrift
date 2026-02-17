"""Add page type column.

Revision ID: b4c2d6e7f8a9
Revises: a3a1b5151573
Create Date: 2026-02-17
"""

from alembic import op
import sqlalchemy as sa

revision = "b4c2d6e7f8a9"
down_revision = "a3a1b5151573"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("type", sa.String(50), nullable=False, server_default="page"),
    )
    op.execute("UPDATE pages SET type = 'page'")
    op.create_index(
        "ix_pages_type_published",
        "pages",
        ["type", "is_published"],
    )


def downgrade() -> None:
    op.drop_index("ix_pages_type_published", table_name="pages")
    op.drop_column("pages", "type")
