"""Add provider_email_verified column to oauth_accounts.

Records whether the upstream provider attested that the email on this OAuth
account is verified at link time. Historic rows default to ``False`` — they
are treated as unverified for any future auto-link decision.

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth_accounts",
        sa.Column(
            "provider_email_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("oauth_accounts", "provider_email_verified")
