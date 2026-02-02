"""add page revisions table

Revision ID: 6f7g8h9i0j1k
Revises: 5e6f7g8h9i0j
Create Date: 2026-02-02 10:03:00.000000

This migration creates the page_revisions table for tracking content history.
Revisions are created when page content is modified, allowing restoration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import advanced_alchemy.types


# revision identifiers, used by Alembic.
revision: str = '6f7g8h9i0j1k'
down_revision: Union[str, None] = '5e6f7g8h9i0j'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('page_revisions',
        sa.Column('id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
        sa.Column('page_id', advanced_alchemy.types.guid.GUID(length=16), nullable=False),
        sa.Column('user_id', advanced_alchemy.types.guid.GUID(length=16), nullable=True),
        sa.Column('revision_number', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
        sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True),
        sa.Column('updated_at', advanced_alchemy.types.datetime.DateTimeUTC(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], name=op.f('fk_page_revisions_page_id_pages'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_page_revisions_user_id_users'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_page_revisions'))
    )
    op.create_index(op.f('ix_page_revisions_page_id'), 'page_revisions', ['page_id'], unique=False)
    op.create_index(op.f('ix_page_revisions_user_id'), 'page_revisions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_page_revisions_user_id'), table_name='page_revisions')
    op.drop_index(op.f('ix_page_revisions_page_id'), table_name='page_revisions')
    op.drop_table('page_revisions')
