"""add SEO fields to pages

Revision ID: 3c4d5e6f7g8h
Revises: 2b3c4d5e6f7g
Create Date: 2026-02-02 10:00:00.000000

This migration adds SEO metadata fields to the pages table:
- meta_description: Meta description for search engines
- og_title: OpenGraph title override
- og_description: OpenGraph description override
- og_image: OpenGraph image URL
- meta_robots: Meta robots directive (noindex, nofollow, etc.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c4d5e6f7g8h'
down_revision: Union[str, None] = '2b3c4d5e6f7g'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('pages') as batch_op:
        batch_op.add_column(sa.Column('meta_description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('og_title', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('og_description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('og_image', sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column('meta_robots', sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('pages') as batch_op:
        batch_op.drop_column('meta_robots')
        batch_op.drop_column('og_image')
        batch_op.drop_column('og_description')
        batch_op.drop_column('og_title')
        batch_op.drop_column('meta_description')
