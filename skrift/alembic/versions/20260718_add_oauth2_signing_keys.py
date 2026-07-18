"""Add oauth2_signing_keys table for asymmetric access-token signing.

Stores the rotating EC P-256 key pairs used to sign OAuth2 access-token
JWTs. The active key signs new tokens; retired keys stay published at the
JWKS endpoint until they age past the access-token lifetime.

Revision ID: a1c2e3f4b5d6
Revises: 9f8e7d6c5b4a
Create Date: 2026-07-18
"""

from alembic import op
import sqlalchemy as sa

revision = "a1c2e3f4b5d6"
down_revision = "9f8e7d6c5b4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth2_signing_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kid", sa.String(64), nullable=False),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("algorithm", sa.String(16), nullable=False, server_default="ES256"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_oauth2_signing_keys_kid",
        "oauth2_signing_keys",
        ["kid"],
        unique=True,
    )
    op.create_index(
        "ix_oauth2_signing_keys_is_active",
        "oauth2_signing_keys",
        ["is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_oauth2_signing_keys_is_active", table_name="oauth2_signing_keys")
    op.drop_index("ix_oauth2_signing_keys_kid", table_name="oauth2_signing_keys")
    op.drop_table("oauth2_signing_keys")
