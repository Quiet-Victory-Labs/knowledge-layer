"""Keep paged discovery job results distinct from legacy semantic snapshots.

Revision ID: 0014_inventory_results
Revises: 0013_autonomy
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, String

revision = "0014_inventory_results"
down_revision = "0013_autonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_refresh_jobs", Column("result_inventory_id", String(64), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Inventory result history is forward-only; restore a verified database backup")
