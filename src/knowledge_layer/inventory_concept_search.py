"""Bounded retrieval of current published local inventory meanings.

The SQL page is metadata only. Exact evidence is resolved for selected results
through the existing concept workflow, then every selected packet is rechecked
after assembly. Native records, formulas and source permissions gain no authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from time import monotonic

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import DBAPIError

from knowledge_layer.access_log import record_source_read
from knowledge_layer.connectors.common import normalize_name
from knowledge_layer.inventory import _encoded, inventory_digest
from knowledge_layer.inventory_citations import (
    inventory_citation_operation,
    validate_inventory_reference,
)
from knowledge_layer.inventory_concepts import InventoryConcepts, _digest
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeValidationError,
    validate_tenant_id,
)

MAX_CANDIDATES = 256
MAX_SELECTED = 20
MAX_RESPONSE_BYTES = 256 * 1024


@contextmanager
def _query_budget(store, conn):
    """Bound registry SQL work on both supported registry backends."""
    if store.backend == "postgresql":
        conn.exec_driver_sql("SET LOCAL statement_timeout = '1500ms'")
        try:
            yield
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", getattr(exc.orig, "pgcode", None)) == "57014":
                raise KnowledgeConflict("Inventory retrieval SQL budget exceeded; narrow the query") from None
            raise
    else:
        driver = conn.connection.driver_connection
        deadline = monotonic() + 1.5
        driver.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
        try:
            yield
        except DBAPIError as exc:
            if "interrupted" in str(exc.orig).lower():
                raise KnowledgeConflict("Inventory retrieval SQL budget exceeded; narrow the query") from None
            raise
        finally:
            driver.set_progress_handler(None, 0)


def _check_response(value):
    if len(_encoded(value)) > MAX_RESPONSE_BYTES:
        raise KnowledgeValidationError("Inventory retrieval exceeds 256 KiB; narrow the query or source scope")
    return value


class InventoryConceptSearch:
    """Use indexed source/head/review gates without loading legacy snapshots."""

    def __init__(self, store):
        self.store = store
        self.concepts = InventoryConcepts(store)
        self.inventory = self.concepts.inventory

    def candidates(self, tenant, query, connection_id=None, limit=MAX_CANDIDATES):
        tenant = validate_tenant_id(tenant)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise KnowledgeValidationError("query must be between 1 and 200 characters")
        if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
            raise KnowledgeValidationError("inventory candidate limit must be 1-256")
        p = self.store.inventory_concept_proposals.c
        d = self.store.decisions.c
        b = self.store.inventory_concept_publications.c
        r = self.store.inventory_runs.c
        h = self.store.inventory_heads.c
        e = self.store.inventory_entities.c
        proof = self.store.inventory_evidence.c
        s = self.store.connections.c
        j = self.store.refresh_jobs.c
        statement = select(self.store.inventory_concept_proposals,
                           s.name.label("connection_name")).join(self.store.connections,
            and_(s.tenant_id == p.tenant_id, s.id == p.connection_id)).join(self.store.decisions,
            and_(d.tenant_id == p.tenant_id, d.connection_id == p.connection_id,
                 d.proposal_id == p.id, d.decision == "approve", d.proposal_digest == p.proposal_digest)
        ).join(self.store.inventory_concept_publications,
            and_(b.tenant_id == p.tenant_id, b.connection_id == p.connection_id,
                 b.concept_id == p.id, b.proposal_digest == p.proposal_digest, b.revoked_at.is_(None))
        ).join(self.store.inventory_runs,
            and_(r.tenant_id == p.tenant_id, r.connection_id == p.connection_id,
                 r.id == p.inventory_id, r.status == "committed",
                 p.evidence_ref["manifest_digest"].as_string() == r.manifest_digest)
        ).join(self.store.inventory_heads,
            and_(h.tenant_id == p.tenant_id, h.connection_id == p.connection_id,
                 h.scope_digest == r.scope_digest, h.policy_digest == r.policy_digest,
                 h.inventory_id == r.id, h.manifest_digest == r.manifest_digest)
        ).join(self.store.inventory_entities,
            and_(e.inventory_id == r.id, e.id == p.evidence_ref["entity_id"].as_string(),
                 e.digest == p.entity_digest)
        ).join(self.store.inventory_evidence,
            and_(proof.inventory_id == r.id, proof.id == p.evidence_ref["evidence_id"].as_string(),
                 proof.digest == p.evidence_digest)
        ).where(p.tenant_id == tenant, p.archived_at.is_(None), s.status != "error",
            self.store._access_clause(tenant, p.connection_id),
            ~exists(select(j.id).where(j.tenant_id == tenant, j.connection_id == p.connection_id,
                                      j.status.in_(("queued", "running", "retry_wait")))))
        if connection_id is not None:
            statement = statement.where(p.connection_id == connection_id)
        words = sorted(set(normalize_name(query).split()))[:40]
        # OR-prefilter allows reordered/plural suggestions; the shared lexical
        # matcher confirms meaningful tokens after authorization, before selection.
        needles = {word[:-1] if len(word) > 4 and word.endswith("s") else word for word in words}
        text = func.lower(p.name + " " + p.definition)
        statement = statement.where(or_(False, *(text.like(
            "%" + word.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%",
            escape="\\") for word in sorted(needles))))
        statement = statement.order_by((func.lower(p.name) == query.casefold()).desc(), p.connection_id, p.id)
        with self.inventory._current_read_connection() as conn, _query_budget(self.store, conn):
            presence = self._present(conn, tenant, connection_id)
            rows = conn.execute(statement.limit(limit)).mappings().all() if needles else []
            qualified = {presence["key"]: True} if presence["key"] else {}
            result = []
            for raw in rows:
                row = dict(raw)
                ref = row["evidence_ref"]
                try:
                    validate_inventory_reference(ref)
                except KnowledgeError:
                    continue
                key = (row["connection_id"], row["inventory_id"], ref["manifest_digest"])
                try:
                    if (_digest(row) != row["proposal_digest"] or ref["connection_id"] != key[0]
                            or ref["inventory_id"] != key[1]):
                        continue
                    if key not in qualified:
                        self.inventory._current_committed(conn, tenant, *key)
                        self.concepts._no_active_refresh(conn, tenant, key[0])
                        qualified[key] = True
                except KnowledgeError:
                    qualified[key] = False
                if qualified.get(key):
                    result.append(row)
            # Earlier source checks must still be valid after metadata assembly.
            for key, valid in qualified.items():
                if valid:
                    self.inventory._current_committed(conn, tenant, *key)
                    self.concepts._no_active_refresh(conn, tenant, key[0])
                    record_source_read(tenant, key[0])
        present = any(qualified.values())
        return self._metadata(tenant, query, connection_id, limit, result,
                              len(rows) == limit or presence["limit_reached"], present, presence["limit_reached"])

    def _present(self, conn, tenant, connection_id):
        """Qualify bounded current head handles, including zero-match inventories."""
        h, r = self.store.inventory_heads.c, self.store.inventory_runs.c
        s, j = self.store.connections.c, self.store.refresh_jobs.c
        statement = select(h.connection_id, h.inventory_id, h.manifest_digest).join(
            self.store.inventory_runs, and_(r.id == h.inventory_id, r.tenant_id == h.tenant_id,
                r.connection_id == h.connection_id, r.status == "committed", r.manifest_digest == h.manifest_digest)
        ).join(self.store.connections, and_(s.tenant_id == h.tenant_id, s.id == h.connection_id)).where(
            h.tenant_id == tenant, s.status != "error", self.store._access_clause(tenant, h.connection_id),
            ~exists(select(j.id).where(j.tenant_id == tenant, j.connection_id == h.connection_id,
                                      j.status.in_(("queued", "running", "retry_wait")))))
        if connection_id is not None:
            statement = statement.where(h.connection_id == connection_id)
        heads = conn.execute(statement.order_by(h.connection_id, h.inventory_id).limit(MAX_CANDIDATES)).all()
        for cid, inventory_id, manifest in heads:
            try:
                self.inventory._current_committed(conn, tenant, cid, inventory_id, manifest)
                self.concepts._no_active_refresh(conn, tenant, cid)
                return {"key": (cid, inventory_id, manifest), "limit_reached": len(heads) == MAX_CANDIDATES}
            except KnowledgeError:
                continue
        return {"key": None, "limit_reached": len(heads) == MAX_CANDIDATES}

    @staticmethod
    def _metadata(tenant, query, connection_id, limit, items, limit_reached, inventory_present, presence_limit_reached):
        digest = inventory_digest({"tenant_id": tenant, "query": query,
            "connection_id": connection_id, "limit": limit, "items": items,
            "limit_reached": limit_reached, "inventory_present": inventory_present,
            "presence_limit_reached": presence_limit_reached})
        return {"items": items, "limit_reached": limit_reached, "candidate_digest": digest,
                "inventory_present": inventory_present, "presence_limit_reached": presence_limit_reached}

    def resolve(self, tenant, row):
        packet = self.concepts.get(tenant, row["connection_id"], concept_id=row["id"])
        if (packet["proposal_digest"] != row["proposal_digest"]
                or packet["evidence_ref"] != row["evidence_ref"]
                or packet["entity_digest"] != row["entity_digest"]
                or packet["evidence_digest"] != row["evidence_digest"]):
            raise KnowledgeConflict("Inventory concept changed after candidate retrieval")
        return packet

    def recheck(self, tenant, packets):
        if len(packets) > MAX_SELECTED:
            raise KnowledgeValidationError("Inventory retrieval selects at most 20 exact concepts")
        for packet in packets:
            current = self.concepts.get(tenant, packet["concept"]["connection_id"], concept_id=packet["id"])
            if inventory_digest(current) != inventory_digest(packet):
                raise KnowledgeConflict("Inventory concept changed during retrieval assembly")


@inventory_citation_operation()
def resolve_inventory_term(store, tenant_id, term, connection_id=None, *, recheck=True):
    """Return current local inventory meanings in the normal term envelope."""
    from knowledge_layer.retrieval import context_text_match

    search = InventoryConceptSearch(store)
    metadata = search.candidates(tenant_id, term, connection_id)
    direct, suggestions = [], []
    for row in metadata["items"]:
        match = context_text_match(term, name=row["name"], text=f"{row['name']} {row['definition']}")
        if match:
            (direct if match["result_kind"] == "direct_match" else suggestions).append((match, row))
    priority = lambda item: (item[0]["match_basis"] != "exact_name", -item[0]["score"], item[1]["id"])
    direct.sort(key=priority)
    suggestions.sort(key=priority)
    matches = [search.resolve(tenant_id, row) for _, row in direct[:10]]
    candidates = [{"candidate_type": "inventory_concept", **match,
                   "context": search.resolve(tenant_id, row)} for match, row in suggestions[:10]]
    packets = matches + [item["context"] for item in candidates]
    result = {"tenant_id": tenant_id, "term": term,
        **({"connection_id": connection_id} if connection_id else {}),
        "matches": matches, "total_matches": len(direct), "more_available": len(direct) > 10,
        "retrieval_candidates": candidates, "retrieval_candidate_total_matches": len(suggestions),
        "retrieval_candidate_more_available": len(suggestions) > 10,
        "inventory_candidate_limit_reached": metadata["limit_reached"],
        "inventory_candidate_digest": metadata["candidate_digest"],
        "inventory_present": metadata["inventory_present"],
        "inventory_candidates_checked": len(metadata["items"]),
        "inventory_search_complete": not metadata["limit_reached"],
        "counts_basis": "current_authorized_bounded_metadata_candidates",
        "retrieval_policy": "Published inventory meanings are local definitions. Text suggestions require confirmation; native formulas and cross-source joins are not authorized."}
    _check_response(result)
    if recheck:
        search.recheck(tenant_id, packets)
    return result
