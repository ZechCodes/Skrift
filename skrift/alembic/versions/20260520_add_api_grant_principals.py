"""Add API grant principal metadata.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-05-20
"""

from alembic import op
import sqlalchemy as sa
from advanced_alchemy.types import GUID


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(
            sa.Column("principal_type", sa.String(length=20), nullable=False, server_default="user"),
        )
        batch_op.add_column(sa.Column("service_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("service_url", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("parent_api_key_id", GUID(length=16), nullable=True))
        batch_op.add_column(sa.Column("grant_source", sa.String(length=50), nullable=True))
        batch_op.create_index("ix_api_keys_parent_api_key_id", ["parent_api_key_id"])
        batch_op.create_foreign_key(
            "fk_api_keys_parent_api_key_id_api_keys",
            "api_keys",
            ["parent_api_key_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_constraint("fk_api_keys_parent_api_key_id_api_keys", type_="foreignkey")
        batch_op.drop_index("ix_api_keys_parent_api_key_id")
        batch_op.drop_column("grant_source")
        batch_op.drop_column("parent_api_key_id")
        batch_op.drop_column("service_url")
        batch_op.drop_column("service_name")
        batch_op.drop_column("principal_type")
