"""Add Dynamic Client Registration metadata to oauth2_clients.

Adds the RFC 7591 columns used by POST /oauth/register: a flag marking a
client as dynamically registered, its token-endpoint auth method, and the
provenance/liveness timestamps the pruning job reads (issued-at, last-used,
registering IP).

Revision ID: b2d3e4f5a6c7
Revises: a1c2e3f4b5d6
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "b2d3e4f5a6c7"
down_revision = "a1c2e3f4b5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth2_clients",
        sa.Column(
            "is_dynamically_registered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "oauth2_clients",
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(64),
            nullable=False,
            server_default="client_secret_post",
        ),
    )
    op.add_column(
        "oauth2_clients",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "oauth2_clients",
        sa.Column("registered_by_ip", sa.String(45), nullable=True),
    )
    op.add_column(
        "oauth2_clients",
        sa.Column("client_id_issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_oauth2_clients_registered_by_ip",
        "oauth2_clients",
        ["registered_by_ip"],
    )


def downgrade() -> None:
    op.drop_index("ix_oauth2_clients_registered_by_ip", table_name="oauth2_clients")
    op.drop_column("oauth2_clients", "client_id_issued_at")
    op.drop_column("oauth2_clients", "registered_by_ip")
    op.drop_column("oauth2_clients", "last_used_at")
    op.drop_column("oauth2_clients", "token_endpoint_auth_method")
    op.drop_column("oauth2_clients", "is_dynamically_registered")
