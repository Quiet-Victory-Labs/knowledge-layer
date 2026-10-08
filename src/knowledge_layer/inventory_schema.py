"""Shared registry/migration tables for staged source-scoped discovery inventories."""

from __future__ import annotations

from sqlalchemy import JSON, Column, ForeignKeyConstraint, Index, Integer, MetaData, String, Table


def inventory_tables(metadata: MetaData) -> dict[str, Table]:
    """Register once; large estates are rows, never a legacy aggregate snapshot JSON."""
    runs = Table(
        "knowledge_inventory_runs", metadata,
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("connection_id", String(64), nullable=False),
        Column("status", String(24), nullable=False),
        Column("owner_digest", String(64), nullable=False),
        Column("owner_subject", String(255), nullable=False),
        Column("claim", JSON),
        Column("source_identity_digest", String(64), nullable=False),
        Column("source_policy_digest", String(64), nullable=False),
        Column("scope", JSON, nullable=False), Column("scope_digest", String(64), nullable=False),
        Column("policy", JSON, nullable=False), Column("policy_digest", String(64), nullable=False),
        Column("watermark", JSON, nullable=False), Column("watermark_digest", String(64), nullable=False),
        Column("limits", JSON, nullable=False), Column("expected_counts", JSON, nullable=False),
        Column("base_generation", Integer, nullable=False),
        Column("previous_inventory_id", String(64)),
        Column("page_count", Integer, nullable=False),
        Column("entity_count", Integer, nullable=False),
        Column("evidence_count", Integer, nullable=False),
        Column("byte_count", Integer, nullable=False),
        Column("next_cursor", String(2048)),
        Column("terminal_seen", Integer, nullable=False),
        Column("manifest_digest", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("completed_at", String(40)),
        Index("ix_inventory_runs_source", "tenant_id", "connection_id", "scope_digest", "policy_digest"),
    )
    pages = Table(
        "knowledge_inventory_pages", metadata,
        Column("inventory_id", String(64), primary_key=True),
        Column("page_index", Integer, primary_key=True),
        Column("cursor", String(2048)), Column("next_cursor", String(2048)),
        Column("digest", String(64), nullable=False), Column("byte_count", Integer, nullable=False),
        Column("entity_count", Integer, nullable=False), Column("evidence_count", Integer, nullable=False),
        ForeignKeyConstraint(["inventory_id"], [runs.c.id], ondelete="CASCADE"),
    )
    entities = Table(
        "knowledge_inventory_entities", metadata,
        Column("inventory_id", String(64), primary_key=True),
        Column("id", String(256), primary_key=True),
        Column("page_index", Integer, nullable=False),
        Column("kind", String(64), nullable=False),
        Column("payload", JSON, nullable=False), Column("evidence_ids", JSON, nullable=False),
        Column("digest", String(64), nullable=False),
        ForeignKeyConstraint(["inventory_id"], [runs.c.id], ondelete="CASCADE"),
    )
    evidence = Table(
        "knowledge_inventory_evidence", metadata,
        Column("inventory_id", String(64), primary_key=True),
        Column("id", String(256), primary_key=True),
        Column("page_index", Integer, nullable=False),
        Column("kind", String(64), nullable=False), Column("payload", JSON, nullable=False),
        Column("digest", String(64), nullable=False),
        ForeignKeyConstraint(["inventory_id"], [runs.c.id], ondelete="CASCADE"),
    )
    heads = Table(
        "knowledge_inventory_heads", metadata,
        Column("tenant_id", String(64), primary_key=True),
        Column("connection_id", String(64), primary_key=True),
        Column("scope_digest", String(64), primary_key=True),
        Column("policy_digest", String(64), primary_key=True),
        Column("generation", Integer, nullable=False),
        Column("inventory_id", String(64), nullable=False),
        Column("manifest_digest", String(64), nullable=False),
        Column("last_complete_scope_at", String(40), nullable=False),
        ForeignKeyConstraint(["inventory_id"], [runs.c.id]),
    )
    return {"inventory_runs": runs, "inventory_pages": pages, "inventory_entities": entities,
            "inventory_evidence": evidence, "inventory_heads": heads}
