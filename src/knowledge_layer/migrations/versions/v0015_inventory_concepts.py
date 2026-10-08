"""Reviewed local concepts over inventory-only sources.

Revision ID: 0015_inventory_concepts
Revises: 0014_inventory_results
"""

from alembic import op
from sqlalchemy import MetaData

from knowledge_layer.inventory_concept_schema import inventory_concept_tables

revision = "0015_inventory_concepts"
down_revision = "0014_inventory_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData()
    inventory_concept_tables(metadata)
    for table in metadata.sorted_tables:
        table.create(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Inventory concept history is forward-only; restore a verified database backup")
