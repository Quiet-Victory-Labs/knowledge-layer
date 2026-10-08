"""Reviewed, scoped business meaning over paged inventory observations.

Immutable proposals reuse registry decisions, source locks, grants and audit.
Publication applies to one local definition, never the native inventory, its
permissions, formulas or source execution. No legacy snapshot is manufactured.
"""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import exists, func, insert, select, update

from knowledge_layer.access_log import record_source_read
from knowledge_layer.inventory import InventoryStore, _encoded, _int, _text, inventory_digest
from knowledge_layer.inventory_citations import (
    inventory_citation_operation,
    resolve_inventory_citation,
    validate_inventory_reference,
)
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeValidationError,
    utc_now,
    validate_tenant_id,
)

PREFIX = "inventory_concept_"
KINDS = frozenset({"business_term", "entity", "metric_definition", "data_product"})
_CONTENT = ("id", "tenant_id", "connection_id", "inventory_id", "name", "definition", "concept_kind",
            "evidence_ref", "entity_digest", "evidence_digest", "rationale", "created_at", "submitted_by")


def _digest(row):
    return inventory_digest({key: row[key] for key in _CONTENT})


def _rationale(value, *, minimum=8):
    _text(value, "local rationale", 1000)
    if len(value.strip()) < minimum:
        raise KnowledgeValidationError(f"Local rationale must be {minimum}-1000 characters")
    return value.strip()


class InventoryConcepts:
    """A typed registry overlay; upstream inventory records remain unchanged."""

    def __init__(self, store):
        self.store = store
        self.inventory = InventoryStore(store)
        self.proposals = store.inventory_concept_proposals
        self.publications = store.inventory_concept_publications

    def _keys(self, tenant, cid, concept_id):
        if not isinstance(concept_id, str) or not re.fullmatch(PREFIX + r"[0-9a-f]{32}", concept_id):
            raise KnowledgeValidationError("An exact local inventory concept ID is required")
        return (self.proposals.c.tenant_id == tenant, self.proposals.c.connection_id == cid,
                self.proposals.c.id == concept_id)

    def _state(self, conn, tenant, cid, concept_id):
        row = conn.execute(select(self.proposals).where(*self._keys(tenant, cid, concept_id))).mappings().first()
        if row is None or row["archived_at"] is not None:
            raise KnowledgeNotFound("Inventory concept was not found")
        row = dict(row)
        if _digest(row) != row["proposal_digest"]:
            raise KnowledgeConflict("Local inventory concept integrity check failed")
        validate_inventory_reference(row["evidence_ref"])
        if row["evidence_ref"]["connection_id"] != cid or row["evidence_ref"]["inventory_id"] != row["inventory_id"]:
            raise KnowledgeConflict("Local inventory concept source binding changed")
        decision = conn.execute(select(self.store.decisions).where(
            self.store.decisions.c.tenant_id == tenant, self.store.decisions.c.connection_id == cid,
            self.store.decisions.c.proposal_id == concept_id,
        )).mappings().first()
        publication = conn.execute(select(self.publications).where(
            self.publications.c.tenant_id == tenant, self.publications.c.connection_id == cid,
            self.publications.c.concept_id == concept_id,
        )).mappings().first()
        return row, dict(decision) if decision else None, dict(publication) if publication else None

    def _resolve(self, tenant, row):
        selected, proof = resolve_inventory_citation(self.store, tenant, row["evidence_ref"])
        if selected["entity"]["digest"] != row["entity_digest"] or proof["digest"] != row["evidence_digest"]:
            raise KnowledgeConflict("Local inventory concept evidence changed")
        return selected

    @staticmethod
    def _approved(row, decision):
        return bool(decision and decision["decision"] == "approve"
                    and decision["proposal_digest"] == row["proposal_digest"])

    def _packet(self, row, decision, publication, selected):
        approved = self._approved(row, decision)
        published = bool(approved and publication and publication["revoked_at"] is None
                         and publication["proposal_digest"] == row["proposal_digest"])
        state = "published" if published else "withdrawn" if publication and publication["revoked_at"] else "unpublished"
        concept = {"id": row["id"], "connection_id": row["connection_id"], "name": row["name"],
                   "definition": row["definition"], "semantic_type": row["concept_kind"],
                   "definition_status": "approved" if approved else "proposed",
                   "trust_state": "locally_reviewed" if approved else "pending_review",
                   "basis": "inventory", "locally_executable": False}
        return {"schema_version": "knowledge-inventory-concept-v1", "basis": "inventory",
                "id": row["id"], "concept": concept, "proposal_digest": row["proposal_digest"],
                "rationale": row["rationale"], "created_at": row["created_at"], "submitted_by": row["submitted_by"],
                "evidence_ref": deepcopy(row["evidence_ref"]), "entity_digest": row["entity_digest"],
                "evidence_digest": row["evidence_digest"], "inventory": selected["inventory"],
                "decision": deepcopy(decision), "publication": deepcopy(publication), "publication_state": state,
                "local_meaning_status": "reviewed" if approved else "proposed",
                "definition_authority": "locally_reviewed" if approved else "unreviewed_proposal",
                "source_text_authority": "untrusted_evidence", "native_authority": "untrusted_evidence",
                "native_meaning_certified": False, "locally_executable": False,
                "source_reads_executed": False, "source_agent_ready": False,
                "relationships": [], "cross_source_candidates": []}

    def _final(self, conn, tenant, cid, original):
        observed = self._state(conn, tenant, cid, original[0]["id"])
        if inventory_digest(observed) != inventory_digest(original):
            raise KnowledgeConflict("Local inventory concept changed during context assembly")
        self._resolve(tenant, observed[0])

    def _no_active_refresh(self, conn, tenant, cid):
        jobs = self.store.refresh_jobs
        if conn.execute(select(jobs.c.id).where(jobs.c.tenant_id == tenant,
                jobs.c.connection_id == cid, jobs.c.status.in_(("queued", "running", "retry_wait")))
                .limit(1)).first():
            raise KnowledgeConflict("Inventory concept use is blocked by an active refresh")

    def _finish(self, tenant, cid, original, selected, *, manage=False):
        """A committed mutation is history; its returned authority is current."""
        packet = self._packet(*original, selected)
        with self.inventory._current_read_connection() as conn:
            self.inventory._source(conn, tenant, cid)
            if manage:
                self.store._require_source_manager()
                self.store._require_access(tenant, cid, manage=True)
            self._final(conn, tenant, cid, original)
            if packet["publication_state"] == "published":
                self._no_active_refresh(conn, tenant, cid)
        if len(_encoded(packet)) > 256 * 1024:
            raise KnowledgeValidationError("Concept context exceeds 256 KiB; narrow the inventory scope")
        return packet

    @inventory_citation_operation()
    def propose(self, tenant_id, connection_id, *, evidence_ref, name, definition, concept_kind, rationale):
        tenant = validate_tenant_id(tenant_id)
        ref = validate_inventory_reference(evidence_ref)
        if ref["connection_id"] != connection_id:
            raise KnowledgeValidationError("A primary concept must cite its own inventory source")
        _text(name, "local concept name", 240)
        _text(definition, "local concept definition", 4000)
        if not isinstance(concept_kind, str) or concept_kind not in KINDS:
            raise KnowledgeValidationError("Unsupported local inventory concept kind")
        rationale = _rationale(rationale)
        principal = self.store._principal()
        actor = principal.subject if principal else "operator"
        with self.inventory._write_transaction() as conn:
            # Contributors may propose with read access, just as for snapshots.
            self.store._lock_source(conn, tenant, connection_id, manage=False)
            self.store._require_proposal_capacity(conn, tenant, connection_id, actor=actor, steward=bool(
                principal is None or self.store.source_admin_group in principal.groups))
            selected, proof = resolve_inventory_citation(self.store, tenant, ref)
            row = {"id": PREFIX + uuid4().hex, "tenant_id": tenant, "connection_id": connection_id,
                   "inventory_id": ref["inventory_id"], "name": name.strip(), "definition": definition.strip(),
                   "concept_kind": concept_kind, "evidence_ref": ref, "entity_digest": selected["entity"]["digest"],
                   "evidence_digest": proof["digest"], "rationale": rationale,
                   "created_at": utc_now(), "submitted_by": actor,
                   "archived_at": None, "archived_by": None, "archive_reason": None}
            row["proposal_digest"] = _digest(row)
            conn.execute(insert(self.proposals).values(**row))
            self.store._audit(conn, tenant, connection_id, "proposal.submit", actor,
                              {"proposal_id": row["id"], "proposal_type": "inventory_concept",
                               "proposal_digest": row["proposal_digest"]})
        return self._finish(tenant, connection_id, (row, None, None), selected)

    @inventory_citation_operation()
    def decide(self, tenant_id, connection_id, *, concept_id, decision, rationale=None):
        tenant = validate_tenant_id(tenant_id)
        principal = self.store._require_source_manager()
        if decision not in {"approve", "reject"}:
            raise KnowledgeValidationError("decision must be 'approve' or 'reject'")
        if rationale is not None:
            _text(rationale, "review rationale", 1000)
        actor = principal.subject if principal else "operator"
        with self.inventory._write_transaction() as conn:
            self.store._lock_source(conn, tenant, connection_id)
            row, previous, publication = self._state(conn, tenant, connection_id, concept_id)
            selected = self._resolve(tenant, row)
            if decision == "approve":
                self._no_active_refresh(conn, tenant, connection_id)
            if previous and (previous["decision"] != decision or previous["proposal_digest"] != row["proposal_digest"]):
                raise KnowledgeConflict("Inventory concept already has a different recorded decision")
            values = previous or {"tenant_id": tenant, "connection_id": connection_id,
                "proposal_id": concept_id, "decision": decision, "rationale": rationale,
                "decided_at": utc_now(), "proposal_digest": row["proposal_digest"]}
            if previous is None:
                conn.execute(insert(self.store.decisions).values(**values))
                self.store._audit(conn, tenant, connection_id, "proposal.decide", actor,
                                  {"proposal_id": concept_id, "decision": decision,
                                   "proposal_digest": row["proposal_digest"],
                                   "rationale_digest": inventory_digest(rationale)})
        return {"proposal": self._finish(tenant, connection_id, (row, values, publication), selected, manage=True),
                "decision": values}

    @inventory_citation_operation()
    def publish(self, tenant_id, connection_id, *, concept_id, expected_proposal_digest, rationale):
        tenant = validate_tenant_id(tenant_id)
        actor = self.store._publication_actor()
        rationale = _rationale(rationale, minimum=16)
        with self.inventory._write_transaction() as conn:
            self.store._lock_source(conn, tenant, connection_id)
            row, decision, previous = self._state(conn, tenant, connection_id, concept_id)
            if expected_proposal_digest != row["proposal_digest"]:
                raise KnowledgeConflict("Local inventory concept changed before publication")
            selected = self._resolve(tenant, row)
            if not self._approved(row, decision):
                raise KnowledgeConflict("Inventory concept requires local owner approval before publication")
            self._no_active_refresh(conn, tenant, connection_id)
            if previous and previous["revoked_at"] is not None:
                raise KnowledgeConflict("This inventory concept was withdrawn; submit a fresh proposal")
            values = previous or {"tenant_id": tenant, "connection_id": connection_id, "concept_id": concept_id,
                "proposal_digest": row["proposal_digest"], "published_by": actor, "published_at": utc_now(),
                "rationale": rationale, "revoked_by": None, "revoked_at": None, "revoke_reason": None}
            if previous is None:
                conn.execute(insert(self.publications).values(**values))
                self.store._audit(conn, tenant, connection_id, "inventory_concept.publish", actor,
                                  {"concept_id": concept_id, "proposal_digest": row["proposal_digest"],
                                   "rationale_digest": inventory_digest(rationale)})
        return self._finish(tenant, connection_id, (row, decision, values), selected, manage=True)

    def withdraw(self, tenant_id, connection_id, *, concept_id, reason):
        tenant = validate_tenant_id(tenant_id)
        actor = self.store._publication_actor()
        reason = _rationale(reason)
        # Withdrawal remains available after source outage/profile/policy change.
        # Return only the withdrawal handle, never retained native/local text.
        with self.inventory._write_transaction() as conn:
            self.store._lock_source(conn, tenant, connection_id)
            keys = self._keys(tenant, connection_id, concept_id)
            if conn.execute(select(self.proposals.c.id).where(*keys)).first() is None:
                raise KnowledgeNotFound("Inventory concept was not found")
            pub_keys = (self.publications.c.tenant_id == tenant, self.publications.c.connection_id == connection_id,
                        self.publications.c.concept_id == concept_id)
            existing = conn.execute(select(self.publications).where(*pub_keys)).mappings().first()
            if existing is None:
                raise KnowledgeNotFound("Inventory concept publication was not found")
            when = existing["revoked_at"] or utc_now()
            if existing["revoked_at"] is None:
                conn.execute(update(self.publications).where(*pub_keys).values(
                    revoked_by=actor, revoked_at=when, revoke_reason=reason))
                self.store._audit(conn, tenant, connection_id, "inventory_concept.withdraw", actor,
                                  {"concept_id": concept_id, "reason_digest": inventory_digest(reason)})
        return {"concept_id": concept_id, "publication_state": "withdrawn", "revoked_at": when}

    @inventory_citation_operation()
    def get(self, tenant_id, connection_id, *, concept_id):
        tenant = validate_tenant_id(tenant_id)
        with self.inventory._current_read_connection() as conn:
            self.inventory._source(conn, tenant, connection_id)
            original = self._state(conn, tenant, connection_id, concept_id)
            selected = self._resolve(tenant, original[0])
            result = self._packet(*original, selected)
            if result["publication_state"] != "published":
                raise KnowledgeNotFound("A current published inventory concept was not found")
            self._final(conn, tenant, connection_id, original)
            self._no_active_refresh(conn, tenant, connection_id)
        return result

    def find(self, tenant_id, concept_id, *, connection_id=None):
        """Resolve a local ID without scanning sources or disclosing denied text."""
        tenant = validate_tenant_id(tenant_id)
        with self.inventory._current_read_connection() as conn:
            statement = select(self.proposals.c.connection_id).where(
                self.proposals.c.tenant_id == tenant, self.proposals.c.id == concept_id,
                self.proposals.c.archived_at.is_(None),
                self.store._access_clause(tenant, self.proposals.c.connection_id))
            if connection_id is not None:
                statement = statement.where(self.proposals.c.connection_id == connection_id)
            cid = conn.execute(statement).scalar_one_or_none()
        if cid is None:
            return None
        try:
            return self.get(tenant, cid, concept_id=concept_id)
        except KnowledgeNotFound:
            return None

    @inventory_citation_operation()
    def page(self, tenant_id, connection_id, *, inventory_id, expected_manifest_digest,
             after=None, limit=10, include_drafts=False, query=None):
        tenant = validate_tenant_id(tenant_id)
        _int(limit, "concept page limit", 20, 1)
        if not isinstance(expected_manifest_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_digest):
            raise KnowledgeValidationError("Concept pages require an exact manifest digest")
        if type(include_drafts) is not bool:
            raise KnowledgeValidationError("include_drafts must be a boolean")
        if query is not None:
            _text(query, "concept query", 200)
        pins = inventory_digest([tenant, connection_id, inventory_id, expected_manifest_digest, include_drafts, query])
        last = None
        if after is not None:
            try:
                _text(after, "concept cursor", 1024)
                decoded = json.loads(base64.urlsafe_b64decode(after.encode()).decode())
                if set(decoded) != {"scope", "after"} or decoded["scope"] != pins:
                    raise ValueError
                last = decoded["after"]
                self._keys(tenant, connection_id, last)
            except (ValueError, TypeError, UnicodeError, KnowledgeValidationError) as exc:
                raise KnowledgeValidationError("Concept cursor belongs to a different scope or query") from exc
        with self.inventory._current_read_connection() as conn:
            _, head = self.inventory._current_committed(conn, tenant, connection_id, inventory_id, expected_manifest_digest)
            if include_drafts:
                self.store._require_source_manager()
                self.store._require_access(tenant, connection_id, manage=True)
            statement = select(self.proposals.c.id).where(self.proposals.c.tenant_id == tenant,
                self.proposals.c.connection_id == connection_id, self.proposals.c.inventory_id == inventory_id,
                self.proposals.c.archived_at.is_(None))
            if not include_drafts:
                statement = statement.where(exists(select(self.publications.c.concept_id).where(
                    self.publications.c.concept_id == self.proposals.c.id, self.publications.c.revoked_at.is_(None))))
            if query is not None:
                pattern = "%" + query.casefold().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
                statement = statement.where(func.lower(self.proposals.c.name + " " + self.proposals.c.definition)
                                            .like(pattern, escape="\\"))
            if last:
                statement = statement.where(self.proposals.c.id > last)
            if self.store.backend == "postgresql":
                conn.exec_driver_sql("SET LOCAL statement_timeout = '1500ms'")
            ids = list(conn.execute(statement.order_by(self.proposals.c.id).limit(limit + 1)).scalars())
            originals, items = [], []
            for concept_id in ids[:limit]:
                original = self._state(conn, tenant, connection_id, concept_id)
                selected = self._resolve(tenant, original[0])
                packet = self._packet(*original, selected)
                originals.append(original)
                if include_drafts or packet["publication_state"] == "published":
                    items.append(packet)
            for original in originals:
                self._final(conn, tenant, connection_id, original)
            _, observed = self.inventory._current_committed(conn, tenant, connection_id, inventory_id, expected_manifest_digest)
            if observed["generation"] != head["generation"]:
                raise KnowledgeConflict("Inventory changed during concept page assembly")
            if include_drafts:
                self.store._require_source_manager()
                self.store._require_access(tenant, connection_id, manage=True)
            self._no_active_refresh(conn, tenant, connection_id)
            record_source_read(tenant, connection_id)
            cursor = base64.urlsafe_b64encode(_encoded({"scope": pins, "after": ids[limit - 1]})).decode() if len(ids) > limit else None
            result = {"schema_version": "knowledge-inventory-concepts-page-v1", "basis": "inventory",
                      "connection_id": connection_id, "inventory_id": inventory_id,
                      "manifest_digest": expected_manifest_digest, "items": items, "next_cursor": cursor,
                      "source_agent_ready": False, "source_reads_executed": False}
            if len(_encoded(result)) > 256 * 1024:
                raise KnowledgeValidationError("Concept page exceeds 256 KiB; narrow the page")
            return result
