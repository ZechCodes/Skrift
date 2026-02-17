"""add mode column to stored_notifications

Revision ID: 8h9i0j1k2l3m
Revises: 7g8h9i0j1k2l
Create Date: 2026-02-17 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8h9i0j1k2l3m'
down_revision: Union[str, None] = '7g8h9i0j1k2l'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'stored_notifications',
        sa.Column('mode', sa.String(length=20), nullable=False, server_default='queued'),
    )
    op.create_index(
        'ix_stored_notifications_scope_sid_mode_notified',
        'stored_notifications',
        ['scope', 'scope_id', 'mode', 'notified_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_stored_notifications_scope_sid_mode_notified',
        table_name='stored_notifications',
    )
    op.drop_column('stored_notifications', 'mode')
