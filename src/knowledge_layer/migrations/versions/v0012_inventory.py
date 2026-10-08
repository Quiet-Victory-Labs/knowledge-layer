"""Paged discovery inventory rows and atomic scoped complete manifests.

Revision ID: 0012_inventory
Revises: 0011_company_guidance
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import MetaData

from knowledge_layer.inventory_schema import inventory_tables

revision = "0012_inventory"
down_revision = "0011_company_guidance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData()
    inventory_tables(metadata)
    for table in metadata.sorted_tables:
        table.create(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Inventory manifests are forward-only; restore a verified database backup")
