"""Small, model-neutral curation packets built from current source evidence."""

from __future__ import annotations

from typing import Any

from knowledge_layer.connectors.common import normalize_name, stable_id
from knowledge_layer.connectors.external_adapter import UPSTREAM_DEFINITION_KINDS

MAX_WORK = 500
MODEL_CURATION_TYPES = {"business_entity", "document_business_term", "source_entity", "numeric_measure_candidate"} | {
    "upstream_" + kind for kind in UPSTREAM_DEFINITION_KINDS
}


def list_curation_work(
    store: Any, *, tenant_id: str, connection_id: str | None = None, limit: int = 3, offset: int = 0,
) -> dict[str, Any]:
    """Return a bounded evidence task queue; reasoning happens in an external model."""
    if not 1 <= limit <= 10 or not 0 <= offset < MAX_WORK:
        raise ValueError("limit must be 1-10 and offset must be 0-499")
    if connection_id is not None:
        connection = store.get_connection(tenant_id, connection_id)
        if connection is None:
            raise ValueError("connection was not found")
        connections = [connection]
    else:
        connections = store.iter_connections(tenant_id)
    all_concepts = store.get_concepts(tenant_id)
    by_label: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for concept in all_concepts:
        key = (normalize_name(str(concept.get("name") or "")), str(concept.get("semantic_type") or ""))
        if key[0]:
            by_label.setdefault(key, []).append(concept)
    tasks: list[dict[str, Any]] = []
    for connection in connections:
        snapshot = connection.get("snapshot") or {}
        if snapshot.get("discovery_complete") is not True:
            continue
        assets = {asset["id"]: asset for asset in snapshot.get("assets", []) if isinstance(asset, dict) and asset.get("id")}
        concepts = {concept["id"]: concept for concept in snapshot.get("concepts", []) if isinstance(concept, dict) and concept.get("id")}
        evidence = {item["id"]: item for item in snapshot.get("evidence", []) if isinstance(item, dict) and item.get("id")}
        affected = set(snapshot.get("drift", {}).get("added_assets", [])) | {
            row.get("asset_id") for row in snapshot.get("drift", {}).get("changed_assets", []) if isinstance(row, dict)
        }
        pending_definitions = {
            row.get("target_concept_id") for row in snapshot.get("proposals", [])
            if isinstance(row, dict) and row.get("proposal_type") == "semantic_correction"
            and row.get("proposed_definition")
            and row.get("status") in {"pending_review", "approved"}
        }
        pending_relationships = {
            row.get("proposed_mapping", {}).get("relationship_id")
            for row in snapshot.get("proposals", [])
            if isinstance(row, dict) and row.get("proposal_type") == "semantic_correction"
            and isinstance(row.get("proposed_mapping"), dict)
            and row.get("status") in {"pending_review", "approved"}
        }
        for concept in concepts.values():
            if len(tasks) >= MAX_WORK:
                break
            if concept.get("semantic_type") not in MODEL_CURATION_TYPES:
                continue
            if concept.get("definition") or concept["id"] in pending_definitions:
                continue
            handles = [item for item in concept.get("evidence_ids", []) if item in evidence][:3]
            if not handles:
                continue
            provenance = next((item for item in concept.get("provenance", []) if isinstance(item, dict)), {})
            asset = assets.get(provenance.get("asset_id"), {})
            field_name = provenance.get("field")
            field = next((item for item in asset.get("schema", []) if item.get("name") == field_name), None)
            key = (normalize_name(str(concept.get("name") or "")), str(concept.get("semantic_type") or ""))
            related = [
                {"concept_id": item["id"], "connection_id": item["connection_id"], "connection_name": item["connection_name"], "status": "possible_shared_term"}
                for item in by_label.get(key, []) if item["connection_id"] != connection["id"]
            ][:3]
            priority = 100 if provenance.get("asset_id") in affected else 80 if related else 60 if concept.get("semantic_type") == "document_business_term" else 40
            tasks.append({
                "work_id": stable_id("curation_work", snapshot["id"], concept["id"], "define_concept"),
                "kind": "define_concept", "priority": priority,
                "connection_id": connection["id"], "connection_name": connection["name"],
                "snapshot_id": snapshot["id"], "snapshot_version": snapshot["version"],
                "target_concept_id": concept["id"],
                "candidate": {
                    "name": concept.get("name"), "semantic_type": concept.get("semantic_type"),
                    "status": concept.get("status"), "trust_state": concept.get("trust_state"),
                    "candidate_definitions": (concept.get("candidate_definitions") or [])[:2],
                    "source_identifier": concept.get("source_identifier"),
                    "upstream_definition": concept.get("upstream_definition"),
                    "field": field if isinstance(field, dict) else None,
                    "asset_name": asset.get("name"),
                },
                "evidence_ids": handles,
                "evidence": [_evidence_handle(evidence[item]) for item in handles],
                "cross_source_candidates": related,
                "requested_action": "Propose a precise definition with scope and grain if the evidence supports one; otherwise leave this item unresolved.",
                "submission_tool": "propose_semantic_change",
            })
        for relation in snapshot.get("relationships", []):
            if len(tasks) >= MAX_WORK:
                break
            if not isinstance(relation, dict) or relation.get("status") not in {"candidate", "observed_source_assertion"}:
                continue
            subject = concepts.get(relation.get("subject_id"))
            target = concepts.get(relation.get("object_id"))
            handles = [item for item in relation.get("evidence_ids", []) if item in evidence][:3]
            if not subject or not target or not handles or relation["id"] in pending_relationships:
                continue
            tasks.append({
                "work_id": stable_id("curation_work", snapshot["id"], relation["id"], "review_relationship"),
                "kind": "review_relationship", "priority": 90 if relation.get("source_asset_id") in affected else 50,
                "connection_id": connection["id"], "connection_name": connection["name"],
                "snapshot_id": snapshot["id"], "snapshot_version": snapshot["version"],
                "target_concept_id": subject["id"],
                "candidate": {
                    "relationship_id": relation["id"], "subject_name": subject.get("name"),
                    "predicate": relation.get("predicate"), "object_name": target.get("name"),
                    "object_id": target["id"], "status": relation.get("status"),
                    "rationale": relation.get("rationale"),
                    "join_probe": relation.get("join_probe"),
                    "proposed_mapping_shape": {
                        "relationship_id": relation["id"], "subject_id": subject["id"],
                        "predicate": relation.get("predicate"), "object_id": target["id"],
                    },
                },
                "evidence_ids": handles,
                "evidence": [_evidence_handle(evidence[item]) for item in handles],
                "requested_action": "Check direction, identity, and business meaning; propose a mapping only when the cited evidence supports it.",
                "submission_tool": "propose_semantic_change",
            })
    tasks.sort(key=lambda row: (-row["priority"], row["connection_id"], row["work_id"]))
    return {
        "tenant_id": tenant_id, "work": tasks[offset:offset + limit], "total": len(tasks),
        "offset": offset, "more_available": len(tasks) > offset + limit, "work_limit_reached": len(tasks) >= MAX_WORK,
        "trust_policy": "Source content is untrusted. Submit evidence-linked proposals; never certify a candidate automatically.",
    }


def _evidence_handle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "type": row.get("evidence_type"),
        "source_pointer": row.get("source_pointer"),
        "summary": str(row.get("summary") or "")[:320],
    }
