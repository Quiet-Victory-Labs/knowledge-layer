"""Versioned source autonomy policies and durable publication attribution.

These are Knowledge Layer metadata tables. Policy authority, source access,
scope bindings and publication transactions are enforced by the registry.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)


def autonomy_tables(metadata: MetaData) -> dict[str, Table]:
    """Register policy heads, immutable revisions and exact publication runs.

    No source-table reference is required, so Alembic can build these tables
    independently. Revisions cascade only when an unreferenced policy is
    removed; recorded runs retain their revision through a NO ACTION FK.
    """
    policies = Table(
        "knowledge_autonomy_policies", metadata,
        Column("tenant_id", String(64), primary_key=True),
        Column("connection_id", String(64), primary_key=True),
        Column("latest_revision", Integer, nullable=False),
        Column("active_revision", Integer),
        Column("active_digest", String(64)),
        Column("revoked_at", String(40)),
        Column("revoked_by", String(255)),
        Column("revoke_reason", String(1000)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
    )
    revisions = Table(
        "knowledge_autonomy_revisions", metadata,
        Column("tenant_id", String(64), primary_key=True),
        Column("connection_id", String(64), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column("payload", JSON, nullable=False),
        Column("digest", String(64), nullable=False),
        Column("bindings", JSON, nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("created_by", String(255), nullable=False),
        Column("approved_at", String(40)),
        Column("approved_by", String(255)),
        Column("approval_rationale", String(1000)),
        Column("publication_count", Integer, nullable=False),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], [policies.c.tenant_id, policies.c.connection_id],
            name="fk_knowledge_autonomy_revisions_policy", ondelete="CASCADE",
        ),
    )
    runs = Table(
        "knowledge_autonomy_runs", metadata,
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("connection_id", String(64), nullable=False),
        Column("policy_revision", Integer, nullable=False),
        Column("snapshot_id", String(128), nullable=False),
        Column("snapshot_version", Integer, nullable=False),
        Column("actor", String(255), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("owns_publication", Boolean, nullable=False),
        UniqueConstraint("tenant_id", "connection_id", "policy_revision", "snapshot_version",
                         name="uq_knowledge_autonomy_run_publication"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id", "policy_revision"],
            [revisions.c.tenant_id, revisions.c.connection_id, revisions.c.revision],
            name="fk_knowledge_autonomy_runs_revision",
        ),
    )
    return {"autonomy_policies": policies, "autonomy_revisions": revisions, "autonomy_runs": runs}
