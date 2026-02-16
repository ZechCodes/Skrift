"""add access_token and refresh_token to oauth_accounts

Revision ID: 8h9i0j1k2l3m
Revises: 7g8h9i0j1k2l
Create Date: 2026-02-16 10:00:00.000000
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
    op.add_column('oauth_accounts', sa.Column('access_token', sa.String(2048), nullable=True))
    op.add_column('oauth_accounts', sa.Column('refresh_token', sa.String(2048), nullable=True))


def downgrade() -> None:
    op.drop_column('oauth_accounts', 'refresh_token')
    op.drop_column('oauth_accounts', 'access_token')
