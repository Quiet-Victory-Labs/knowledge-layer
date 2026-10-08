"""Add explicit source metadata grants.

Revision ID: 0002_source_grants
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_source_grants"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_source_grants",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("connection_id", sa.String(64), sa.ForeignKey("knowledge_connections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("principal_type", sa.String(16), primary_key=True),
        sa.Column("principal_id", sa.String(255), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("granted_at", sa.String(40), nullable=False),
        sa.Column("granted_by", sa.String(255), nullable=False),
    )
    op.create_index(
        "ix_knowledge_source_grants_lookup", "knowledge_source_grants",
        ["tenant_id", "principal_type", "principal_id", "role"],
    )


def downgrade() -> None:
    raise RuntimeError("Source grant history is forward-only; restore a verified database backup")
