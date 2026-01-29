"""add provider_metadata column to oauth_accounts

Revision ID: 2b3c4d5e6f7g
Revises: 1a2b3c4d5e6f
Create Date: 2026-01-29 12:00:00.000000

This migration adds a JSON column to store the full raw OAuth provider response.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b3c4d5e6f7g'
down_revision: Union[str, None] = '1a2b3c4d5e6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('oauth_accounts') as batch_op:
        batch_op.add_column(sa.Column('provider_metadata', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('oauth_accounts') as batch_op:
        batch_op.drop_column('provider_metadata')
