"""add stored_notifications table

Revision ID: 7g8h9i0j1k2l
Revises: 6f7g8h9i0j1k
Create Date: 2026-02-15 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7g8h9i0j1k2l'
down_revision: Union[str, None] = '6f7g8h9i0j1k'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'stored_notifications',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('scope', sa.String(length=10), nullable=False),
        sa.Column('scope_id', sa.String(length=255), nullable=False),
        sa.Column('type', sa.String(length=100), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('group_key', sa.String(length=255), nullable=True),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_stored_notifications_scope_scope_id',
        'stored_notifications',
        ['scope', 'scope_id'],
    )
    op.create_index(
        'ix_stored_notifications_scope_scope_id_group',
        'stored_notifications',
        ['scope', 'scope_id', 'group_key'],
    )
    op.create_index(
        'ix_stored_notifications_notified_at',
        'stored_notifications',
        ['notified_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_stored_notifications_notified_at', table_name='stored_notifications')
    op.drop_index('ix_stored_notifications_scope_scope_id_group', table_name='stored_notifications')
    op.drop_index('ix_stored_notifications_scope_scope_id', table_name='stored_notifications')
    op.drop_table('stored_notifications')
