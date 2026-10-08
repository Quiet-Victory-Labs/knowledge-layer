"""Freeze the initial seven-table Knowledge Layer registry schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def schema_metadata() -> sa.MetaData:
    """Return this revision's immutable table definition for safe adoption."""
    metadata = sa.MetaData()
    sa.Table(
        "knowledge_connections", metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("last_refreshed_at", sa.String(40)),
        sa.Column("last_error", sa.Text),
    )
    sa.Table(
        "knowledge_snapshots", metadata,
        sa.Column("row_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("connection_id", sa.String(64), nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.UniqueConstraint("tenant_id", "connection_id", "version", name="uq_knowledge_snapshot_version"),
    )
    sa.Table(
        "knowledge_graph_nodes", metadata,
        sa.Column("row_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("node_id", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("label", sa.String(240), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("trust_state", sa.String(40), nullable=False),
        sa.Column("evidence_ids", sa.JSON, nullable=False),
        sa.UniqueConstraint("tenant_id", "connection_id", "version", "node_id", name="uq_knowledge_graph_node"),
        sa.Index("ix_knowledge_graph_nodes_scope", "tenant_id", "connection_id", "version", "kind"),
    )
    sa.Table(
        "knowledge_graph_edges", metadata,
        sa.Column("row_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("edge_id", sa.String(240), nullable=False),
        sa.Column("subject_id", sa.String(160), nullable=False),
        sa.Column("predicate", sa.String(80), nullable=False),
        sa.Column("object_id", sa.String(160), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("trust_state", sa.String(40), nullable=False),
        sa.Column("evidence_ids", sa.JSON, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.UniqueConstraint("tenant_id", "connection_id", "version", "edge_id", name="uq_knowledge_graph_edge"),
        sa.Index("ix_knowledge_graph_edges_subject", "tenant_id", "connection_id", "version", "subject_id"),
        sa.Index("ix_knowledge_graph_edges_object", "tenant_id", "connection_id", "version", "object_id"),
    )
    sa.Table(
        "knowledge_proposal_decisions", metadata,
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("connection_id", sa.String(64), primary_key=True),
        sa.Column("proposal_id", sa.String(128), primary_key=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text),
        sa.Column("decided_at", sa.String(40), nullable=False),
    )
    sa.Table(
        "knowledge_semantic_proposals", metadata,
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("connection_id", sa.String(64), nullable=False, index=True),
        sa.Column("proposal_type", sa.String(64), nullable=False),
        sa.Column("target_concept_id", sa.String(160), nullable=False),
        sa.Column("proposed_definition", sa.Text),
        sa.Column("proposed_mapping", sa.JSON),
        sa.Column("evidence_ids", sa.JSON, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    sa.Table(
        "knowledge_contract_proposals", metadata,
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("connection_id", sa.String(64), nullable=False, index=True),
        sa.Column("source_snapshot_id", sa.String(128), nullable=False),
        sa.Column("assertion_kind", sa.String(40), nullable=False),
        sa.Column("asset_id", sa.String(160), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("evidence_ids", sa.JSON, nullable=False),
        sa.Column("evidence_refs", sa.JSON, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    return metadata


def upgrade() -> None:
    schema_metadata().create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise RuntimeError("The registry baseline is forward-only; restore a verified database backup")
