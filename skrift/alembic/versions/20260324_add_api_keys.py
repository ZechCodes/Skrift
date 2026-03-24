"""Add api_keys table for programmatic authentication.

Revision ID: a7b8c9d0e1f2
Revises: 4ef00221df53
Create Date: 2026-03-24
"""

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC

revision = "a7b8c9d0e1f2"
down_revision = "4ef00221df53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("user_id", GUID(length=16), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("key_prefix", sa.String(12), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("scoped_permissions", sa.Text(), nullable=True),
        sa.Column("scoped_roles", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("last_used_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(45), nullable=True),
        sa.Column("refresh_token_hash", sa.String(128), nullable=True),
        sa.Column("refresh_token_expires_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_refresh_token_hash", "api_keys", ["refresh_token_hash"], unique=True)
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_refresh_token_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
