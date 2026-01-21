"""initial schema

Revision ID: 09b0364dbb7b
Revises:
Create Date: 2026-01-20 21:01:54.470260

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC


# revision identifiers, used by Alembic.
revision: str = '09b0364dbb7b'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create users table
    op.create_table(
        'users',
        sa.Column('id', GUID(length=16), nullable=False),
        sa.Column('created_at', DateTimeUTC(timezone=True), nullable=False),
        sa.Column('updated_at', DateTimeUTC(timezone=True), nullable=False),
        sa.Column('oauth_provider', sa.String(length=50), nullable=False),
        sa.Column('oauth_id', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('picture_url', sa.String(length=512), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, default=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('oauth_id'),
        sa.UniqueConstraint('email'),
    )

    # Create pages table
    op.create_table(
        'pages',
        sa.Column('id', GUID(length=16), nullable=False),
        sa.Column('created_at', DateTimeUTC(timezone=True), nullable=False),
        sa.Column('updated_at', DateTimeUTC(timezone=True), nullable=False),
        sa.Column('user_id', GUID(length=16), nullable=True),
        sa.Column('type', sa.String(length=50), nullable=False, default='page'),
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False, default=''),
        sa.Column('is_published', sa.Boolean(), nullable=False, default=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.UniqueConstraint('slug'),
    )

    # Create indexes
    op.create_index('ix_pages_slug', 'pages', ['slug'])
    op.create_index('ix_pages_user_id', 'pages', ['user_id'])
    op.create_index('ix_pages_type_published', 'pages', ['type', 'is_published'])


def downgrade() -> None:
    op.drop_index('ix_pages_type_published', 'pages')
    op.drop_index('ix_pages_user_id', 'pages')
    op.drop_index('ix_pages_slug', 'pages')
    op.drop_table('pages')
    op.drop_table('users')
