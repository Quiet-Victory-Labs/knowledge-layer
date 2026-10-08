"""Registry-only local meaning and publication over exact inventory observations."""

from sqlalchemy import JSON, Column, ForeignKeyConstraint, Index, MetaData, String, Table, Text


def inventory_concept_tables(metadata: MetaData) -> dict[str, Table]:
    proposals = Table(
        "knowledge_inventory_concept_proposals", metadata,
        Column("id", String(128), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("connection_id", String(64), nullable=False),
        Column("inventory_id", String(64), nullable=False),
        Column("name", String(240), nullable=False),
        Column("definition", Text, nullable=False),
        Column("concept_kind", String(40), nullable=False),
        Column("evidence_ref", JSON, nullable=False),
        Column("entity_digest", String(64), nullable=False),
        Column("evidence_digest", String(64), nullable=False),
        Column("proposal_digest", String(64), nullable=False),
        Column("rationale", String(1000), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("submitted_by", String(255), nullable=False),
        Column("archived_at", String(40)),
        Column("archived_by", String(255)),
        Column("archive_reason", String(1000)),
        Index("ix_inventory_concepts_scope", "tenant_id", "connection_id", "inventory_id", "id"),
        Index("ix_inventory_concepts_submitter", "tenant_id", "connection_id", "submitted_by", "archived_at"),
    )
    publications = Table(
        "knowledge_inventory_concept_publications", metadata,
        Column("concept_id", String(128), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("connection_id", String(64), nullable=False),
        Column("proposal_digest", String(64), nullable=False),
        Column("published_by", String(255), nullable=False),
        Column("published_at", String(40), nullable=False),
        Column("rationale", String(1000), nullable=False),
        Column("revoked_by", String(255)),
        Column("revoked_at", String(40)),
        Column("revoke_reason", String(1000)),
        ForeignKeyConstraint(["concept_id"], [proposals.c.id]),
        Index("ix_inventory_concept_publications_source", "tenant_id", "connection_id"),
    )
    return {"inventory_concept_proposals": proposals, "inventory_concept_publications": publications}
