"""add oauth_accounts table

Revision ID: 1a2b3c4d5e6f
Revises: 8f3a5c2d1e0b
Create Date: 2026-01-29 10:00:00.000000

This migration:
1. Creates the oauth_accounts table to store multiple OAuth identities per user
2. Migrates existing oauth data from users table to oauth_accounts
3. Makes users.email nullable (for providers like Twitter that don't provide email)
4. Removes oauth_provider and oauth_id columns from users table
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC


# revision identifiers, used by Alembic.
revision: str = '1a2b3c4d5e6f'
down_revision: Union[str, None] = '8f3a5c2d1e0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Create oauth_accounts table
    op.create_table(
        'oauth_accounts',
        sa.Column('id', GUID(length=16), nullable=False),
        sa.Column('created_at', DateTimeUTC(timezone=True), nullable=False),
        sa.Column('updated_at', DateTimeUTC(timezone=True), nullable=False),
        sa.Column('sa_orm_sentinel', sa.Integer(), nullable=True),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('provider_account_id', sa.String(length=255), nullable=False),
        sa.Column('provider_email', sa.String(length=255), nullable=True),
        sa.Column('user_id', GUID(length=16), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_oauth_accounts')),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name=op.f('fk_oauth_accounts_user_id_users'),
            ondelete='CASCADE'
        ),
        sa.UniqueConstraint(
            'provider', 'provider_account_id',
            name='uq_oauth_provider_account'
        ),
    )
    op.create_index(
        op.f('ix_oauth_accounts_user_id'),
        'oauth_accounts',
        ['user_id'],
        unique=False
    )
    op.create_index(
        op.f('ix_oauth_accounts_provider_account'),
        'oauth_accounts',
        ['provider', 'provider_account_id'],
        unique=True
    )

    # Step 2: Migrate existing data from users to oauth_accounts
    # Generate binary UUIDs (16 bytes) for new records and copy oauth data
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == 'sqlite':
        uuid_func = 'randomblob(16)'
    else:  # PostgreSQL and others
        uuid_func = 'gen_random_uuid()'

    conn.execute(sa.text(f"""
        INSERT INTO oauth_accounts (id, created_at, updated_at, provider, provider_account_id, provider_email, user_id)
        SELECT
            {uuid_func},
            created_at,
            updated_at,
            oauth_provider,
            oauth_id,
            email,
            id
        FROM users
        WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL
    """))

    # Step 3: Make email nullable on users table
    # SQLite doesn't support ALTER COLUMN, so we need to recreate the table
    # For SQLite, we'll use batch_alter_table
    with op.batch_alter_table('users', schema=None) as batch_op:
        # Drop the unique constraint on oauth_id
        batch_op.drop_constraint('uq_users_oauth_id', type_='unique')
        # Drop the oauth columns
        batch_op.drop_column('oauth_provider')
        batch_op.drop_column('oauth_id')
        # Make email nullable - this requires recreating the column in SQLite
        batch_op.alter_column('email',
                              existing_type=sa.String(length=255),
                              nullable=True)


def downgrade() -> None:
    # Step 1: Add back oauth columns to users table
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('oauth_provider', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('oauth_id', sa.String(length=255), nullable=True))
        batch_op.alter_column('email',
                              existing_type=sa.String(length=255),
                              nullable=False)

    # Step 2: Migrate data back from oauth_accounts to users
    # Only migrate the first oauth account per user
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE users
        SET oauth_provider = (
            SELECT provider FROM oauth_accounts
            WHERE oauth_accounts.user_id = users.id
            LIMIT 1
        ),
        oauth_id = (
            SELECT provider_account_id FROM oauth_accounts
            WHERE oauth_accounts.user_id = users.id
            LIMIT 1
        )
    """))

    # Step 3: Make oauth columns non-nullable and add unique constraint
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('oauth_provider',
                              existing_type=sa.String(length=50),
                              nullable=False)
        batch_op.alter_column('oauth_id',
                              existing_type=sa.String(length=255),
                              nullable=False)
        batch_op.create_unique_constraint('uq_users_oauth_id', ['oauth_id'])

    # Step 4: Drop oauth_accounts table
    op.drop_index(op.f('ix_oauth_accounts_provider_account'), table_name='oauth_accounts')
    op.drop_index(op.f('ix_oauth_accounts_user_id'), table_name='oauth_accounts')
    op.drop_table('oauth_accounts')
