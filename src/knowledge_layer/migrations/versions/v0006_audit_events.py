"""Append-only metadata governance events for operator export.

Revision ID: 0006_audit_events
Revises: 0005_decision_digests
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_audit_events"
down_revision = "0005_decision_digests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_audit_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64)),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("occurred_at", sa.String(40), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_knowledge_audit_export", "knowledge_audit_events", ["tenant_id", "occurred_at", "id"])
    op.create_index("ix_knowledge_audit_source", "knowledge_audit_events", ["tenant_id", "connection_id", "occurred_at"])


def downgrade() -> None:
    raise RuntimeError("Audit history is forward-only; restore a verified database backup")
