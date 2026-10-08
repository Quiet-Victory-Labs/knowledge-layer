"""Add a seek index for tenant-scoped connection pagination.

Revision ID: 0008_source_paging_index
Revises: 0007_proposal_lifecycle
"""

from __future__ import annotations

from alembic import op

revision = "0008_source_paging_index"
down_revision = "0007_proposal_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_connections_tenant_created_id",
        "knowledge_connections",
        ["tenant_id", "created_at", "id"],
    )


def downgrade() -> None:
    raise RuntimeError("Registry schema is forward-only; restore a verified database backup")
