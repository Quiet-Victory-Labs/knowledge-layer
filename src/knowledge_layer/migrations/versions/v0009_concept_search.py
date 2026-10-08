"""Index current concepts for bounded, grant-filtered agent search.

Revision ID: 0009_concept_search
Revises: 0008_source_paging_index
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

from knowledge_layer.concept_index import records

revision = "0009_concept_search"
down_revision = "0008_source_paging_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "knowledge_concept_search",
        sa.Column("row_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("snapshot_version", sa.Integer, nullable=False),
        sa.Column("concept_id", sa.String(160), nullable=False),
        sa.Column("name", sa.String(240), nullable=False),
        sa.Column("definition", sa.String(320)),
        sa.Column("semantic_type", sa.String(80)),
        sa.Column("trust_state", sa.String(40)),
        sa.Column("evidence_ids", sa.JSON, nullable=False),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("search_text", sa.Text, nullable=False),
        sa.UniqueConstraint("tenant_id", "connection_id", "concept_id",
                            name="uq_knowledge_concept_search_current"),
    )
    op.create_index("ix_knowledge_concept_search_scope", "knowledge_concept_search",
                    ["tenant_id", "connection_id"])
    # Existing installations retain only one search row per current concept.
    # Seek-page the backfill so upgrade memory does not track historical snapshots.
    connection = op.get_bind()
    latest_page = sa.text("""
        SELECT current.tenant_id, current.connection_id, current.version, current.payload,
               (SELECT json_agg(p.id)
                FROM knowledge_semantic_proposals p
                WHERE p.tenant_id = current.tenant_id
                  AND p.connection_id = current.connection_id
                  AND p.id IN (SELECT coalesce(item ->> 'id', item ->> 'proposal_id')
                               FROM jsonb_array_elements(
                                   coalesce(current.payload::jsonb -> 'proposals', '[]'::jsonb)
                               ) item)
                  AND p.archived_at IS NOT NULL) AS archived_ids,
               (SELECT json_agg(d.proposal_id)
                FROM knowledge_proposal_decisions d
                WHERE d.tenant_id = current.tenant_id
                  AND d.connection_id = current.connection_id
                  AND d.decision = 'approve') AS approved_ids,
               (SELECT json_agg(json_build_object(
                   'target_concept_id', p.target_concept_id,
                   'proposed_definition', p.proposed_definition))
                FROM knowledge_semantic_proposals p
                JOIN knowledge_proposal_decisions d
                  ON d.tenant_id = p.tenant_id
                 AND d.connection_id = p.connection_id
                 AND d.proposal_id = p.id
                 AND d.decision = 'approve'
                WHERE p.tenant_id = current.tenant_id
                  AND p.connection_id = current.connection_id
                  AND p.archived_at IS NULL
                  AND p.proposed_definition IS NOT NULL) AS local_proposals
        FROM (
            SELECT DISTINCT ON (tenant_id, connection_id)
                   tenant_id, connection_id, version, payload
            FROM knowledge_snapshots
            WHERE (tenant_id, connection_id) > (:last_tenant, :last_source)
            ORDER BY tenant_id, connection_id, version DESC
            LIMIT 10
        ) current
    """)
    batch = []
    last_tenant = last_source = ""
    while True:
        page = connection.execute(latest_page, {
            "last_tenant": last_tenant, "last_source": last_source,
        }).mappings().all()
        if not page:
            break
        for snapshot in page:
            payload = snapshot["payload"] or {}
            approved = set(snapshot["approved_ids"] or [])
            archived_local = set(snapshot["archived_ids"] or [])
            for concept in payload.get("concepts") or []:
                if not isinstance(concept, dict):
                    continue
                marker = concept.get("definition_proposal_id")
                if marker and (marker not in approved or marker in archived_local):
                    concept.pop("definition", None)
            alternate_terms: dict[str, list[str]] = {}
            for proposal in payload.get("proposals") or []:
                proposal_id = str(proposal.get("id") or proposal.get("proposal_id")) if isinstance(proposal, dict) else ""
                if (not isinstance(proposal, dict)
                        or proposal_id not in approved
                        or proposal_id in archived_local):
                    continue
                definition = proposal.get("proposed_definition")
                if proposal.get("target_concept_id") and isinstance(definition, str):
                    alternate_terms.setdefault(str(proposal["target_concept_id"]), []).append(
                        definition[:400]
                    )
            for proposal in snapshot["local_proposals"] or []:
                if isinstance(proposal, dict) and proposal.get("target_concept_id"):
                    definition = proposal.get("proposed_definition")
                    if isinstance(definition, str):
                        alternate_terms.setdefault(str(proposal["target_concept_id"]), []).append(
                            definition[:400]
                        )
            for record in records(payload):
                batch.append({
                    "row_id": uuid4().hex, "tenant_id": snapshot["tenant_id"],
                    "connection_id": snapshot["connection_id"],
                    "snapshot_version": snapshot["version"],
                    "concept_id": record["concept_id"], "name": record["name"],
                    "definition": record["definition"],
                    "semantic_type": record["semantic_type"],
                    "trust_state": record["trust_state"],
                    "evidence_ids": record["evidence_ids"], "evidence": record["evidence"],
                    "search_text": (record["text"] + " " +
                                    " ".join(alternate_terms.get(record["concept_id"], []))[:1000])[:2001],
                })
                if len(batch) >= 500:
                    connection.execute(sa.insert(table), batch)
                    batch.clear()
        last_tenant, last_source = page[-1]["tenant_id"], page[-1]["connection_id"]
    if batch:
        connection.execute(sa.insert(table), batch)
    op.execute("""
        CREATE INDEX ix_knowledge_concept_search_text_gin
        ON knowledge_concept_search
        USING gin (to_tsvector('simple'::regconfig, search_text))
    """)


def downgrade() -> None:
    raise RuntimeError("Registry schema is forward-only; restore a verified database backup")
