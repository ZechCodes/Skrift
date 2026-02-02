"""add content scheduling

Revision ID: 5e6f7g8h9i0j
Revises: 4d5e6f7g8h9i
Create Date: 2026-02-02 10:02:00.000000

This migration adds the publish_at column for scheduled publishing.
When set, pages with is_published=True will only be visible after this datetime.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import advanced_alchemy.types


# revision identifiers, used by Alembic.
revision: str = '5e6f7g8h9i0j'
down_revision: Union[str, None] = '4d5e6f7g8h9i'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('pages') as batch_op:
        batch_op.add_column(
            sa.Column('publish_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=True)
        )
        batch_op.create_index('ix_pages_publish_at', ['publish_at'])


def downgrade() -> None:
    with op.batch_alter_table('pages') as batch_op:
        batch_op.drop_index('ix_pages_publish_at')
        batch_op.drop_column('publish_at')
