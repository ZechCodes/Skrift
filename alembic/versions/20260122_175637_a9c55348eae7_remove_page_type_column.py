"""remove page type column

Revision ID: a9c55348eae7
Revises: cdf734a5b847
Create Date: 2026-01-22 17:56:37.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c55348eae7'
down_revision: Union[str, None] = 'cdf734a5b847'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Delete any posts (convert to pages not needed since we're removing posts entirely)
    op.execute("DELETE FROM pages WHERE type = 'post'")

    # Drop the composite index on type and is_published
    op.drop_index('ix_pages_type_published', table_name='pages')

    # Drop the type column
    op.drop_column('pages', 'type')


def downgrade() -> None:
    # Add the type column back
    op.add_column('pages', sa.Column('type', sa.String(50), nullable=True))

    # Set all existing pages to type 'page'
    op.execute("UPDATE pages SET type = 'page'")

    # Make the column non-nullable
    op.alter_column('pages', 'type', nullable=False)

    # Recreate the composite index
    op.create_index('ix_pages_type_published', 'pages', ['type', 'is_published'])
