"""Persist recurring refresh schedules and separate refresh clocks.

Revision ID: 0010_refresh_schedules
Revises: 0009_concept_search
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_refresh_schedules"
down_revision = "0009_concept_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_refresh_schedules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64),
                  sa.ForeignKey("knowledge_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("paused", sa.Boolean, nullable=False),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("next_run_at", sa.String(40)),
        sa.Column("last_enqueued_at", sa.String(40)),
        sa.Column("last_job_id", sa.String(64)),
        sa.Column("last_error", sa.String(40)),
        sa.UniqueConstraint("tenant_id", "connection_id",
                            name="uq_knowledge_refresh_schedules_source"),
    )
    op.create_index("ix_knowledge_refresh_schedules_ready", "knowledge_refresh_schedules",
                    ["paused", "next_run_at"])
    op.add_column("knowledge_refresh_jobs", sa.Column("schedule_id", sa.String(64)))
    op.create_foreign_key("fk_knowledge_refresh_jobs_schedule", "knowledge_refresh_jobs",
                          "knowledge_refresh_schedules", ["schedule_id"], ["id"],
                          ondelete="SET NULL")
    op.add_column("knowledge_connections", sa.Column("last_refresh_attempt_at", sa.String(40)))
    op.add_column("knowledge_connections", sa.Column("last_complete_refresh_at", sa.String(40)))


def downgrade() -> None:
    raise RuntimeError("Refresh schedule history is forward-only; restore a verified database backup")
