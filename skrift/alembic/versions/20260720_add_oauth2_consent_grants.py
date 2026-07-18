"""Add oauth2_consent_grants table for remembered consent.

Stores that a user has approved a set of scopes for an OAuth2 client so the
``/oauth/authorize`` consent screen can be skipped when a later request stays
within the granted scopes. One grant per (user, client); deleting a client
cascades to its grants.

Revision ID: c3e4f5a6b7d8
Revises: b2d3e4f5a6c7
Create Date: 2026-07-20
"""

from advanced_alchemy.types import GUID, DateTimeUTC
from alembic import op
import sqlalchemy as sa

revision = "c3e4f5a6b7d8"
down_revision = "b2d3e4f5a6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth2_consent_grants",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column(
            "user_id",
            GUID(length=16),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
        sa.Column("granted_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_id", name="uq_oauth2_consent_grants_user_client"),
    )
    op.create_index("ix_oauth2_consent_grants_user_id", "oauth2_consent_grants", ["user_id"])
    op.create_index("ix_oauth2_consent_grants_client_id", "oauth2_consent_grants", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth2_consent_grants_client_id", table_name="oauth2_consent_grants")
    op.drop_index("ix_oauth2_consent_grants_user_id", table_name="oauth2_consent_grants")
    op.drop_table("oauth2_consent_grants")
