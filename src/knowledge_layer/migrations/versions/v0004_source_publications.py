"""Pin steward publication to one source snapshot version.

Revision ID: 0004_source_publications
Revises: 0003_refresh_jobs
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_source_publications"
down_revision = "0003_refresh_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_source_publications",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("connection_id", sa.String(64),
                  sa.ForeignKey("knowledge_connections.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("snapshot_version", sa.Integer, primary_key=True),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("published_by", sa.String(255), nullable=False),
        sa.Column("published_at", sa.String(40), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("revoked_by", sa.String(255)),
        sa.Column("revoked_at", sa.String(40)),
        sa.Column("revoke_reason", sa.String(1000)),
    )


def downgrade() -> None:
    raise RuntimeError("Publication history is forward-only; restore a verified database backup")
