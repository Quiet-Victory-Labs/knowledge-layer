"""Bounded source autonomy policy revisions and durable publication runs.

Revision ID: 0013_autonomy
Revises: 0012_inventory
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import MetaData

from knowledge_layer.autonomy_schema import autonomy_tables

revision = "0013_autonomy"
down_revision = "0012_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData()
    autonomy_tables(metadata)
    for table in metadata.sorted_tables:
        table.create(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Autonomy policy and run history is forward-only; restore a verified database backup")
