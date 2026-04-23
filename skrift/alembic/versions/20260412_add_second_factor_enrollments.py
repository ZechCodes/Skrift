"""Add second_factor_enrollments table.

Revision ID: c1d2e3f4a5b6
Revises: a7b8c9d0e1f2
Create Date: 2026-04-12
"""

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC

revision = "c1d2e3f4a5b6"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "second_factor_enrollments",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("user_id", GUID(length=16), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("factor_key", sa.String(100), nullable=False),
        sa.Column("factor_type", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("credential_id", sa.String(512), nullable=True),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transports", sa.Text(), nullable=True),
        sa.Column("enrollment_metadata", sa.JSON(), nullable=True),
        sa.Column("enrolled_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("last_used_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "factor_key",
            "credential_id",
            name="uq_second_factor_enrollment_credential",
        ),
    )
    op.create_index(
        "ix_second_factor_enrollments_user_id",
        "second_factor_enrollments",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_second_factor_enrollments_factor_key",
        "second_factor_enrollments",
        ["factor_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_second_factor_enrollments_factor_key", table_name="second_factor_enrollments")
    op.drop_index("ix_second_factor_enrollments_user_id", table_name="second_factor_enrollments")
    op.drop_table("second_factor_enrollments")
