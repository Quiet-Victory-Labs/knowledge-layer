"""Bounded cross-connection projection of explicitly cited source evidence."""

from __future__ import annotations

from typing import Any

from knowledge_layer.connectors.common import stable_id
from knowledge_layer.connectors.external_adapter import UPSTREAM_DEFINITION_KINDS
from knowledge_layer.inventory_citations import inventory_citation_operation
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeValidationError,
)


def _current_document_citations(
    store: Any, *, tenant_id: str, connection_id: str, proposal: dict[str, Any],
    evidence: dict[str, Any], documents: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]] | None:
    """Resolve all explicit citations through current authorized source evidence."""
    refs = proposal.get("evidence_refs") or []
    local_handles = proposal.get("evidence_ids") or []
    if (not refs or not local_handles or not set(local_handles).issubset(evidence)
            or proposal.get("effective_on_current_snapshot") is False):
        return None
    from knowledge_layer.inventory_citations import (
        InventoryCitationBudgetExceeded,
        is_inventory_reference,
        resolve_inventory_citation,
    )

    current_refs = []
    permitted_types = {"document_derived_term", "upstream_semantic_definition"}
    if proposal.get("assertion_kind") == "cross_source_join":
        from knowledge_layer.cross_source import STRUCTURED_EVIDENCE

        permitted_types |= STRUCTURED_EVIDENCE
    for ref in refs:
        if not isinstance(ref, dict):
            return None
        cid = ref.get("connection_id")
        if not isinstance(cid, str) or cid == connection_id:
            return None
        if is_inventory_reference(ref):
            if proposal.get("assertion_kind") == "cross_source_join":
                return None
            try:
                state, item = resolve_inventory_citation(store, tenant_id, ref)
            except InventoryCitationBudgetExceeded:
                raise
            except KnowledgeError:
                return None
            current_refs.append((cid, state, item))
            continue
        if set(ref) != {"connection_id", "snapshot_id", "evidence_id"}:
            return None
        if cid not in documents:
            other = store.get_connection(tenant_id, cid)
            if other and other.get("snapshot"):
                documents[cid] = other, other["snapshot"]
        other_pair = documents.get(cid)
        if not other_pair or other_pair[0].get("status") == "error":
            return None
        other_snapshot = other_pair[1]
        if other_snapshot.get("id") != ref.get("snapshot_id"):
            return None
        other_evidence = next((item for item in other_snapshot.get("evidence", [])
                               if isinstance(item, dict) and item.get("id") == ref.get("evidence_id")
                               and item.get("evidence_type") in permitted_types), None)
        if other_evidence is None:
            return None
        if proposal.get("assertion_kind") == "cross_source_join" and other_snapshot.get("agent_ready") is not True:
            return None
        if other_evidence.get("evidence_type") == "upstream_semantic_definition":
            if _upstream_asset(other_snapshot, other_evidence) is None:
                return None
            if proposal.get("status") == "approved" and not _reviewed_upstream_concepts(other_snapshot, other_evidence):
                return None
        current_refs.append((cid, other_snapshot, other_evidence))
    return current_refs


def _upstream_asset(snapshot: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Verify the exact imported native binding, without treating it as reviewed."""
    asset = next((item for item in snapshot.get("assets", []) if isinstance(item, dict)
                  and item.get("id") == evidence.get("asset_id")
                  and item.get("asset_type") == "semantic_definition"), None)
    if not asset or evidence.get("evidence_type") != "upstream_semantic_definition":
        return None
    native = asset.get("upstream_definition")
    details = evidence.get("details") or {}
    if (not isinstance(native, dict) or not isinstance(details, dict)
            or not isinstance(native.get("kind"), str) or native["kind"] not in UPSTREAM_DEFINITION_KINDS
            or not asset.get("revision") or evidence.get("source_pointer") != asset.get("source_pointer")
            or evidence.get("id") != stable_id("evidence", asset["id"], "upstream_definition", asset["revision"])
            or details.get("source_revision") != asset["revision"]
            or details.get("upstream_definition") != {key: value for key, value in native.items() if key != "source_observed_at"}):
        return None
    return asset


def _reviewed_upstream_concepts(
    snapshot: dict[str, Any], evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Require an effective local meaning review anchored to this native evidence."""
    asset = _upstream_asset(snapshot, evidence)
    if asset is None:
        return []
    native = asset["upstream_definition"]
    handles = {item.get("id") for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    proposals = {item.get("id"): item for item in snapshot.get("proposals", []) if isinstance(item, dict)}
    concepts = []
    for concept in snapshot.get("concepts", []):
        if not isinstance(concept, dict):
            continue
        binding = concept.get("source_binding") or {}
        ids = concept.get("definition_evidence_ids") or []
        review = proposals.get(concept.get("definition_proposal_id")) or {}
        if (concept.get("id") != stable_id("concept", asset["id"], "upstream_definition")
                or concept.get("semantic_type") != "upstream_" + native["kind"]
                or concept.get("upstream_definition") != native
                or binding.get("asset_id") != asset["id"] or binding.get("source_pointer") != asset.get("source_pointer")
                or concept.get("definition_status") != "approved" or not concept.get("definition")
                or evidence["id"] not in ids or not set(ids).issubset(handles)
                or review.get("proposal_type") != "semantic_correction" or review.get("status") != "approved"
                or review.get("effective_on_current_snapshot") is not True
                or review.get("target_concept_id") != concept["id"]
                or review.get("proposed_definition") != concept["definition"]
                or evidence["id"] not in (review.get("evidence_ids") or [])):
            continue
        concepts.append(concept)
    return concepts


def _source_assertion(
    connection_id: str, snapshot: dict[str, Any], proposal: dict[str, Any],
) -> dict[str, Any]:
    assertion = {
        "connection_id": connection_id, "snapshot_id": snapshot.get("id"),
        "snapshot_version": snapshot.get("version"), "assertion_id": proposal["id"],
        "assertion_kind": proposal.get("assertion_kind"), "asset_id": proposal["asset_id"],
        "status": "approved", "effective_on_current_snapshot": True,
        "evidence_ids": proposal["evidence_ids"],
    }
    payload = proposal.get("payload") or {}
    if field := payload.get("field") or payload.get("measure_field"):
        assertion["field"] = field
    return assertion


def cited_glossary(
    store: Any, *, tenant_id: str, connection_id: str, expected_snapshot_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expose reviewed definitions cited by effective source assertions, without equivalence."""
    source = store.get_connection(tenant_id, connection_id)
    if not source or not (snapshot := source.get("snapshot")):
        return []
    if expected_snapshot_id is not None and snapshot.get("id") != expected_snapshot_id:
        return []
    assets = {item.get("id"): item for item in snapshot.get("assets", []) if isinstance(item, dict)}
    evidence = {item.get("id"): item for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    documents: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    entries = []
    proposals = sorted(
        (item for item in snapshot.get("proposals", []) if isinstance(item, dict)
         and item.get("proposal_type") == "contract_assertion" and item.get("status") == "approved"
         and item.get("effective_on_current_snapshot") is True and item.get("asset_id") in assets),
        key=lambda item: str(item.get("id") or ""),
    )
    for proposal in proposals:
        refs = _current_document_citations(
            store, tenant_id=tenant_id, connection_id=connection_id, proposal=proposal,
            evidence=evidence, documents=documents,
        )
        if refs is None:
            continue
        assertion = _source_assertion(connection_id, snapshot, proposal)
        for cid, doc_snapshot, item in refs:
            if item.get("evidence_type") != "document_derived_term":
                continue
            if not any(isinstance(asset, dict) and asset.get("id") == item.get("asset_id")
                       for asset in doc_snapshot.get("assets", [])):
                continue
            doc_evidence = {entry.get("id") for entry in doc_snapshot.get("evidence", [])
                            if isinstance(entry, dict)}
            concepts = sorted(
                (entry for entry in doc_snapshot.get("concepts", []) if isinstance(entry, dict)
                 and entry.get("semantic_type") == "document_business_term"
                 and entry.get("definition_status") == "approved" and entry.get("definition")
                 and item["id"] in (entry.get("definition_evidence_ids") or [])
                 and set(entry["definition_evidence_ids"]).issubset(doc_evidence)),
                key=lambda entry: str(entry.get("id") or ""),
            )
            for concept in concepts:
                entries.append({
                    "concept_id": concept["id"], "term": concept.get("name"),
                    "definition": concept["definition"], "definition_status": "approved",
                    "definition_proposal_id": concept.get("definition_proposal_id"),
                    "trust_state": "owner_reviewed",
                    "concept_trust_state": concept.get("trust_state") or concept.get("status"),
                    "connection_id": cid, "snapshot_id": doc_snapshot.get("id"),
                    "snapshot_version": doc_snapshot.get("version"),
                    "evidence_ref": {"connection_id": cid, "snapshot_id": doc_snapshot.get("id"),
                                     "evidence_id": item["id"]},
                    "evidence_ids": concept["definition_evidence_ids"],
                    "source_pointer": item.get("source_pointer"), "source_assertion": assertion,
                })
    return entries


def cited_upstream_definitions(
    store: Any, *, tenant_id: str, connection_id: str, expected_snapshot_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expose native provenance only through two current, explicit local reviews."""
    source = store.get_connection(tenant_id, connection_id)
    if not source or not (snapshot := source.get("snapshot")):
        return []
    if expected_snapshot_id is not None and snapshot.get("id") != expected_snapshot_id:
        return []
    assets = {item.get("id") for item in snapshot.get("assets", []) if isinstance(item, dict)}
    evidence = {item.get("id"): item for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    sources: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    entries = []
    proposals = sorted(
        (item for item in snapshot.get("proposals", []) if isinstance(item, dict)
         and item.get("proposal_type") == "contract_assertion" and item.get("status") == "approved"
         and item.get("effective_on_current_snapshot") is True and item.get("asset_id") in assets),
        key=lambda item: str(item.get("id") or ""),
    )
    for proposal in proposals:
        refs = _current_document_citations(
            store, tenant_id=tenant_id, connection_id=connection_id, proposal=proposal,
            evidence=evidence, documents=sources,
        )
        if refs is None:
            continue
        assertion = _source_assertion(connection_id, snapshot, proposal)
        for cid, upstream_snapshot, item in refs:
            if item.get("evidence_type") != "upstream_semantic_definition":
                continue
            asset = _upstream_asset(upstream_snapshot, item)
            if asset is None:
                continue
            for concept in _reviewed_upstream_concepts(upstream_snapshot, item):
                entries.append({
                    "concept_id": concept["id"], "term": concept.get("name"),
                    "definition": concept["definition"], "definition_status": "approved",
                    "definition_proposal_id": concept.get("definition_proposal_id"),
                    "trust_state": "owner_reviewed",
                    "concept_trust_state": concept.get("trust_state") or concept.get("status"),
                    "connection_id": cid, "snapshot_id": upstream_snapshot.get("id"),
                    "snapshot_version": upstream_snapshot.get("version"),
                    "evidence_ref": {"connection_id": cid, "snapshot_id": upstream_snapshot.get("id"),
                                     "evidence_id": item["id"]},
                    "evidence_ids": concept["definition_evidence_ids"],
                    "source_pointer": item.get("source_pointer"), "source_revision": asset["revision"],
                    "upstream_definition": dict(asset["upstream_definition"]),
                    "source_assertion": assertion, "executable": False, "certified": False,
                })
    return entries


@inventory_citation_operation()
def cited_inventory_evidence(
    store: Any, *, tenant_id: str, connection_id: str, expected_snapshot_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expose raw inventory citations supporting independently reviewed local assertions.

    These compact handles certify neither the native record nor its permissions,
    formulas or relationships. Complete payloads remain in exact inventory reads.
    """
    source = store.get_connection(tenant_id, connection_id)
    if not source or not (snapshot := source.get("snapshot")):
        return []
    if expected_snapshot_id is not None and snapshot.get("id") != expected_snapshot_id:
        return []
    assets = {item.get("id") for item in snapshot.get("assets", []) if isinstance(item, dict)}
    evidence = {item.get("id"): item for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    sources: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    entries = []
    emitted_proposals = []
    proposals = sorted(
        (item for item in snapshot.get("proposals", []) if isinstance(item, dict)
         and item.get("proposal_type") == "contract_assertion" and item.get("status") == "approved"
         and item.get("effective_on_current_snapshot") is True and item.get("asset_id") in assets),
        key=lambda item: str(item.get("id") or ""),
    )
    for proposal in proposals:
        refs = _current_document_citations(
            store, tenant_id=tenant_id, connection_id=connection_id, proposal=proposal,
            evidence=evidence, documents=sources,
        )
        if refs is None:
            continue
        assertion = _source_assertion(connection_id, snapshot, proposal)
        emitted = False
        for cid, state, item in refs:
            if "inventory_id" not in state:
                continue
            entity = state["entity"]
            ref = {"connection_id": cid, "inventory_id": state["inventory_id"],
                   "manifest_digest": state["manifest_digest"], "entity_id": entity["id"],
                   "evidence_id": item["id"]}
            entries.append({
                "connection_id": cid, "inventory_id": state["inventory_id"],
                "manifest_digest": state["manifest_digest"], "evidence_ref": ref,
                "entity_id": entity["id"], "entity_kind": entity["kind"], "entity_digest": entity["digest"],
                "evidence_id": item["id"], "evidence_digest": item["digest"],
                "source_text_authority": "untrusted_evidence", "native_authority": "untrusted_evidence",
                "meaning_certified": False, "locally_executable": False, "source_assertion": assertion,
            })
            emitted = True
        if emitted:
            emitted_proposals.append(proposal)
    # An earlier citation may be withdrawn while a later proposal is resolved.
    # Recheck entire mixed dependency sets without the earlier snapshot cache.
    for proposal in emitted_proposals:
        if _current_document_citations(
            store, tenant_id=tenant_id, connection_id=connection_id, proposal=proposal,
            evidence=evidence, documents={},
        ) is None:
            return []
    if entries and not store._has_access(tenant_id, connection_id):
        return []
    return entries


@inventory_citation_operation()
def federated_evidence_graph(
    store: Any, *, tenant_id: str, connection_id: str, limit: int = 8,
) -> dict[str, Any]:
    """Show document/import/inventory citations, never inferred cross-source identity."""
    if not 1 <= limit <= 12:
        raise KnowledgeValidationError("Federated graph limit must be 1-12 assertions")
    source = store.get_connection(tenant_id, connection_id)
    if source is None or not (snapshot := source.get("snapshot")):
        raise KnowledgeNotFound("Source snapshot was not found")

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    documents: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def node(cid: str, sid: str, native_id: str, kind: str, label: str, *,
             description: str | None = None, status: str = "observed",
             trust_state: str = "unverified", evidence_ids: list[str] | None = None) -> str:
        node_id = f"{cid}:{native_id}"
        nodes[node_id] = {
            "id": node_id, "connection_id": cid, "snapshot_id": sid,
            "kind": kind, "label": str(label)[:240],
            "description": str(description)[:500] if description else None,
            "status": status, "trust_state": trust_state,
            "evidence_ids": [f"{cid}:{item}" for item in (evidence_ids or [])[:8]],
        }
        return node_id

    def edge(subject: str, predicate: str, object_id: str, status: str, *,
             evidence_ids: list[str] | None = None) -> None:
        if subject not in nodes or object_id not in nodes:
            return
        edge_id = stable_id("federated_edge", subject, predicate, object_id)
        edges[edge_id] = {
            "id": edge_id, "subject_id": subject, "predicate": predicate,
            "object_id": object_id, "status": status,
            "trust_state": "owner_reviewed" if status == "approved" else "candidate",
            "evidence_ids": evidence_ids or [], "confidence": None,
        }

    def inventory_node(cid: str, state: dict[str, Any], record: dict[str, Any], kind: str) -> str:
        # One proof may support multiple entities; bind each exact reference.
        # Hash structured parts so delimiters in opaque IDs cannot alias nodes.
        entity_binding = state["entity"]["id"] if kind == "inventory_evidence" else None
        node_id = f"{cid}:{stable_id('inventory_node', state['inventory_id'], kind, record['id'], entity_binding)}"
        nodes[node_id] = {
            "id": node_id, "connection_id": cid, "inventory_id": state["inventory_id"],
            "manifest_digest": state["manifest_digest"], "kind": kind,
            "record_id": record["id"], "record_kind": record["kind"], "record_digest": record["digest"],
            "label": str(record["id"])[:240], "description": None,
            "status": "observed", "trust_state": "untrusted_evidence",
            "source_text_authority": "untrusted_evidence", "native_authority": "untrusted_evidence",
            "meaning_certified": False,
            "locally_executable": False,
        }
        return node_id

    assets = {item.get("id"): item for item in snapshot.get("assets", []) if isinstance(item, dict)}
    evidence = {item.get("id"): item for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    assertions = [
        item for item in snapshot.get("proposals", [])
        if isinstance(item, dict) and item.get("proposal_type") == "contract_assertion"
        and item.get("evidence_refs") and item.get("status") in {"approved", "pending_review"}
    ]
    assertions.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
    stale_count = 0
    linked_count = 0
    linked_proposals = []
    for proposal in assertions[:limit]:
        asset = assets.get(proposal.get("asset_id"))
        if not asset:
            continue
        payload = proposal.get("payload") or {}
        current_refs = _current_document_citations(
            store, tenant_id=tenant_id, connection_id=connection_id, proposal=proposal,
            evidence=evidence, documents=documents,
        )
        is_current = current_refs is not None
        if not is_current:
            stale_count += 1
        status = "stale" if not is_current else proposal["status"]
        sid = str(snapshot["id"])
        asset_node = node(connection_id, sid, asset["id"], "asset", asset.get("name") or asset["id"],
                          description=asset.get("description"), status="observed", trust_state="observed")
        target_node = asset_node
        if proposal.get("assertion_kind") in {"field_description", "metric"}:
            field_name = payload.get("field") or payload.get("measure_field")
            field = next((item for item in asset.get("schema", []) if item.get("name") == field_name), None)
            if field:
                target_node = node(connection_id, sid, stable_id("field", asset["id"], field_name),
                                   "field", field_name, description=field.get("description"),
                                   status=field.get("description_status") or "observed",
                                   trust_state="owner_reviewed" if field.get("description_status") == "approved" else "observed")
                edge(asset_node, "has_field", target_node, "observed")
        assertion_id = node(
            connection_id, sid, str(proposal["id"]), "assertion",
            str(proposal.get("assertion_kind") or "Assertion").replace("_", " ").title(),
            description=payload.get("description") or payload.get("name") or proposal.get("rationale"),
            status=status, trust_state="owner_reviewed" if status == "approved" else "candidate",
            evidence_ids=proposal.get("evidence_ids"),
        )
        edge(target_node, "has_assertion", assertion_id, status)
        if proposal.get("assertion_kind") == "join_rule" and (target_asset := assets.get(payload.get("to_asset_id"))):
            join_target_node = node(connection_id, sid, target_asset["id"], "asset",
                                    target_asset.get("name") or target_asset["id"],
                                    description=target_asset.get("description"), status="observed", trust_state="observed")
            edge(assertion_id, "joins_to", join_target_node, status)
        for evidence_id in (proposal.get("evidence_ids") or [])[:3]:
            item = evidence.get(evidence_id)
            if not item:
                continue
            evidence_node = node(connection_id, sid, evidence_id, "evidence", "Source evidence",
                                 description=item.get("summary"), status="observed", trust_state="observed")
            edge(assertion_id, "supported_by", evidence_node, status)

        if not is_current:
            continue
        linked_count += 1
        linked_proposals.append(proposal)
        for cid, doc_snapshot, item in current_refs or []:
            if "inventory_id" in doc_snapshot:
                entity_node = inventory_node(cid, doc_snapshot, doc_snapshot["entity"], "inventory_entity")
                proof_node = inventory_node(cid, doc_snapshot, item, "inventory_evidence")
                nodes[proof_node]["evidence_ref"] = {
                    "connection_id": cid, "inventory_id": doc_snapshot["inventory_id"],
                    "manifest_digest": doc_snapshot["manifest_digest"],
                    "entity_id": doc_snapshot["entity"]["id"], "evidence_id": item["id"],
                }
                edge(proof_node, "evidence_for_inventory_entity", entity_node, "observed")
                edge(assertion_id, "cites_inventory_evidence", proof_node, "observed")
                continue
            doc_asset = next((entry for entry in doc_snapshot.get("assets", [])
                              if isinstance(entry, dict) and entry.get("id") == item.get("asset_id")), None)
            if not doc_asset:
                continue
            doc_sid = str(doc_snapshot["id"])
            imported = item.get("evidence_type") == "upstream_semantic_definition"
            observed_trust = "observed_upstream_definition" if imported else "observed_source_text"
            doc_asset_node = node(cid, doc_sid, doc_asset["id"], "asset",
                                  doc_asset.get("name") or doc_asset["id"],
                                  status="observed", trust_state=observed_trust)
            if proposal.get("assertion_kind") == "cross_source_join":
                edge(assertion_id, "cross_source_joins_to", doc_asset_node, status,
                     evidence_ids=[f"{cid}:{item['id']}"])
            doc_evidence_node = node(cid, doc_sid, item["id"], "evidence", "Upstream definition evidence" if imported else "Document evidence",
                                     description=item.get("summary"), status="observed",
                                     trust_state=observed_trust)
            edge(doc_evidence_node, "observed_in", doc_asset_node, "observed")
            edge(assertion_id, "grounded_in", doc_evidence_node, status)
            for concept in [entry for entry in doc_snapshot.get("concepts", [])
                            if isinstance(entry, dict) and item["id"] in (entry.get("evidence_ids") or [])][:2]:
                concept_node = node(cid, doc_sid, concept["id"], "concept",
                                    concept.get("name") or concept["id"],
                                    description=concept.get("definition"),
                                    status=concept.get("definition_status") or concept.get("status") or "candidate",
                                    trust_state=concept.get("trust_state") or "inferred",
                                    evidence_ids=concept.get("evidence_ids"))
                edge(concept_node, "supported_by", doc_evidence_node,
                     "approved" if concept.get("definition_status") == "approved" else "candidate")

    for proposal in linked_proposals:
        if _current_document_citations(
            store, tenant_id=tenant_id, connection_id=connection_id, proposal=proposal,
            evidence=evidence, documents={},
        ) is None:
            raise KnowledgeConflict("Cited sources changed during graph assembly; reload current evidence")
    if not store._has_access(tenant_id, connection_id):
        raise KnowledgeNotFound("Source snapshot was not found")
    return {
        "connection_id": connection_id, "snapshot_id": snapshot["id"],
        "version": snapshot["version"], "focus_id": None,
        "nodes": list(nodes.values()), "edges": list(edges.values()),
        "assertion_count": len(assertions), "linked_assertions": linked_count,
        "stale_citations": stale_count, "truncated": len(assertions) > limit,
        "warning": "Links show explicit source citations, not cross-source entity equivalence.",
    }
