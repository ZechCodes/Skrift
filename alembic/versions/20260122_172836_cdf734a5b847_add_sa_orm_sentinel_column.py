"""add sa_orm_sentinel column

Revision ID: cdf734a5b847
Revises: 0b7c927d2591
Create Date: 2026-01-22 17:28:36.586660

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cdf734a5b847'
down_revision: Union[str, None] = '0b7c927d2591'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add sa_orm_sentinel column to users table
    op.add_column('users', sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True))

    # Add sa_orm_sentinel column to pages table
    op.add_column('pages', sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('pages', 'sa_orm_sentinel')
    op.drop_column('users', 'sa_orm_sentinel')
