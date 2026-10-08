"""Persist bounded source refresh work.

Revision ID: 0003_refresh_jobs
Revises: 0002_source_grants
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_refresh_jobs"
down_revision = "0002_source_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_refresh_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), sa.ForeignKey("knowledge_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("requested_at", sa.String(40), nullable=False),
        sa.Column("next_run_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.String(40)),
        sa.Column("finished_at", sa.String(40)),
        sa.Column("lease_until", sa.String(40)),
        sa.Column("worker_id", sa.String(80)),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        sa.Column("cancel_requested", sa.Boolean, nullable=False),
        sa.Column("result_snapshot_id", sa.String(128)),
        sa.Column("error_code", sa.String(40)),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_knowledge_refresh_jobs_ready", "knowledge_refresh_jobs", ["status", "next_run_at"])
    op.create_index(
        "uq_knowledge_refresh_jobs_active_source", "knowledge_refresh_jobs",
        ["tenant_id", "connection_id"], unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retry_wait')"),
    )


def downgrade() -> None:
    raise RuntimeError("Refresh job history is forward-only; restore a verified database backup")
