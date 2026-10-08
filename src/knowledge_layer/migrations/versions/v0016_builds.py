"""Durable external-agent builds.

Revision ID: 0016_builds
Revises: 0015_inventory_concepts
"""
from alembic import op
from sqlalchemy import MetaData

from knowledge_layer.build_schema import build_tables

revision = "0016_builds"
down_revision = "0015_inventory_concepts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData()
    build_tables(metadata)
    for table in metadata.sorted_tables:
        table.create(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Build receipts are forward-only; restore a verified database backup")
