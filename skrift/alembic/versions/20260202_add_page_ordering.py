"""add page ordering

Revision ID: 4d5e6f7g8h9i
Revises: 3c4d5e6f7g8h
Create Date: 2026-02-02 10:01:00.000000

This migration adds an order column to the pages table for custom ordering.
Lower numbers appear first, default is 0.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4d5e6f7g8h9i'
down_revision: Union[str, None] = '3c4d5e6f7g8h'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('pages') as batch_op:
        batch_op.add_column(sa.Column('order', sa.Integer(), nullable=False, server_default='0'))
        batch_op.create_index('ix_pages_order', ['order'])


def downgrade() -> None:
    with op.batch_alter_table('pages') as batch_op:
        batch_op.drop_index('ix_pages_order')
        batch_op.drop_column('order')
