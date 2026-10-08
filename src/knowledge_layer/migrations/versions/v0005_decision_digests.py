"""Bind owner decisions to reviewed proposal content.

Revision ID: 0005_decision_digests
Revises: 0004_source_publications
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_decision_digests"
down_revision = "0004_source_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy decisions are deliberately unbound and require steward re-review.
    op.add_column("knowledge_proposal_decisions", sa.Column("proposal_digest", sa.String(64)))


def downgrade() -> None:
    raise RuntimeError("Decision bindings are forward-only; restore a verified database backup")
