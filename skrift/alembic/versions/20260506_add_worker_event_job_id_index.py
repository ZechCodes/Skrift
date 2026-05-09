"""Add job id index for worker lifecycle event lookups.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-06
"""

from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_events", sa.Column("job_id", sa.String(64), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("UPDATE worker_events SET job_id = event ->> 'job_id'")
    elif bind.dialect.name == "sqlite":
        op.execute("UPDATE worker_events SET job_id = json_extract(event, '$.job_id')")
    op.create_index(
        "ix_worker_events_stream_job_id_position",
        "worker_events",
        ["stream", "job_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_events_stream_job_id_position", table_name="worker_events")
    op.drop_column("worker_events", "job_id")
