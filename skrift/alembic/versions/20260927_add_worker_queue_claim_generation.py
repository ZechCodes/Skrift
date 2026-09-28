"""Add worker_queue.claim_generation to order the claims of a job.

Each claim increments it and takes the new value as its order, so a worker
whose claim expired and was taken over cannot write over the later claim's run.
Rows already queued start at 0.

Revision ID: 0a173359c9d2
Revises: c3e4f5a6b7d8
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0a173359c9d2"
down_revision = "c3e4f5a6b7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("worker_queue") as batch_op:
        batch_op.add_column(
            sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_queue") as batch_op:
        batch_op.drop_column("claim_generation")
