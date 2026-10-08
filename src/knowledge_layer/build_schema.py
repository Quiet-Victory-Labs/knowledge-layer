"""Durable, bounded customer-agent work and checkpoint receipts."""
from sqlalchemy import (
    JSON,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)


def build_tables(metadata: MetaData) -> dict[str, Table]:
    runs = Table(
        "knowledge_builds", metadata,
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("created_by", String(255), nullable=False),
        Column("connection_ids", JSON, nullable=False),
        Column("sources", JSON, nullable=False),
        Column("status", String(40), nullable=False),
        Column("max_items", Integer, nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        Column("expires_at", String(40), nullable=False),
        Column("coverage", JSON, nullable=False),
        Column("cancel_reason", String(1000)),
        UniqueConstraint("tenant_id", "id", name="uq_knowledge_build_tenant_id"),
    )
    tasks = Table(
        "knowledge_build_tasks", metadata,
        Column("id", String(64), primary_key=True),
        Column("tenant_id", String(64), nullable=False),
        Column("run_id", String(64), nullable=False),
        Column("connection_id", String(64), nullable=False),
        Column("work_id", String(160), nullable=False),
        Column("track", String(16), nullable=False),
        Column("kind", String(40), nullable=False),
        Column("snapshot_id", String(128), nullable=False),
        Column("snapshot_version", Integer, nullable=False),
        Column("packet", JSON, nullable=False),
        Column("status", String(32), nullable=False),
        Column("attempt", Integer, nullable=False),
        Column("position", Integer, nullable=False),
        Column("lease_token", String(64)),
        Column("lease_until", String(40)),
        Column("leased_by", String(255)),
        Column("proposal_id", String(128)),
        Column("reason", String(1000)),
        Column("result_digest", String(64)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        UniqueConstraint("tenant_id", "run_id", "work_id", name="uq_knowledge_build_work"),
        ForeignKeyConstraint(["tenant_id", "run_id"], [runs.c.tenant_id, runs.c.id],
                             name="fk_knowledge_build_task_run", ondelete="CASCADE"),
    )
    Index("ix_knowledge_build_tenant_created", runs.c.tenant_id, runs.c.created_at)
    Index("ix_knowledge_build_task_status", tasks.c.tenant_id, tasks.c.run_id, tasks.c.status)
    return {"builds": runs, "build_tasks": tasks}
