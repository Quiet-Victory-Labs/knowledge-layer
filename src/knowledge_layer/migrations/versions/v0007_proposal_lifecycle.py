"""Bound active proposal projection while preserving archived review history.

Revision ID: 0007_proposal_lifecycle
Revises: 0006_audit_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_proposal_lifecycle"
down_revision = "0006_audit_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, index in (
        ("knowledge_semantic_proposals", "ix_knowledge_semantic_proposals_active_submitter"),
        ("knowledge_contract_proposals", "ix_knowledge_contract_proposals_active_submitter"),
    ):
        op.add_column(table, sa.Column("submitted_by", sa.String(255), nullable=False, server_default="legacy"))
        op.add_column(table, sa.Column("archived_at", sa.String(40)))
        op.add_column(table, sa.Column("archived_by", sa.String(255)))
        op.add_column(table, sa.Column("archive_reason", sa.String(1000)))
        op.create_index(index, table, ["tenant_id", "connection_id", "submitted_by", "archived_at"])


def downgrade() -> None:
    raise RuntimeError("Proposal history is forward-only; restore a verified database backup")
