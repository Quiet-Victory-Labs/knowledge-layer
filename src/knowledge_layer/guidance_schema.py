"""Small metadata tables for immutable company guidance and its publication pointer."""

from __future__ import annotations

from sqlalchemy import JSON, Column, ForeignKeyConstraint, Integer, MetaData, String, Table


def register_guidance_tables(metadata: MetaData) -> tuple[Table, Table]:
    bundles = Table(
        "knowledge_guidance_bundles", metadata,
        Column("tenant_id", String(64), primary_key=True),
        Column("slug", String(80), primary_key=True),
        Column("latest_revision", Integer, nullable=False),
        Column("published_revision", Integer),
        Column("published_digest", String(64)),
        Column("published_at", String(40)),
        Column("published_by", String(255)),
        Column("publication_rationale", String(1000)),
        Column("revoked_at", String(40)),
        Column("revoked_by", String(255)),
        Column("revoke_reason", String(1000)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
    )
    revisions = Table(
        "knowledge_guidance_revisions", metadata,
        Column("tenant_id", String(64), primary_key=True),
        Column("slug", String(80), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column("payload", JSON, nullable=False),
        Column("digest", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("created_by", String(255), nullable=False),
        ForeignKeyConstraint(["tenant_id", "slug"],
                             ["knowledge_guidance_bundles.tenant_id", "knowledge_guidance_bundles.slug"],
                             name="fk_knowledge_guidance_revisions_bundle", ondelete="CASCADE"),
    )
    return bundles, revisions
