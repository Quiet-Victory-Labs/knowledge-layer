"""Compact, evidence-preserving graph projection of a sanitized snapshot."""

from __future__ import annotations

import math
from typing import Any

from knowledge_layer.connectors.common import stable_id

_NODE_COLLECTIONS = (
    ("concepts", "concept"),
    ("metrics", "metric"),
    ("assets", "asset"),
    ("evidence", "evidence"),
)


def project_graph(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project only named records and links with resolvable endpoints.

    The graph is a navigation index. It does not infer business relationships,
    certify candidates, or copy document bodies and sample rows.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for collection, kind in _NODE_COLLECTIONS:
        for record in snapshot.get(collection, []):
            if not isinstance(record, dict):
                continue
            node_id = record.get("id")
            if not isinstance(node_id, str) or not node_id or len(node_id) > 160 or node_id in nodes:
                continue
            label = record.get("name") or record.get("title") or (record.get("evidence_type") if kind == "evidence" else None) or ("Evidence" if kind == "evidence" else node_id)
            description = record.get("definition") or record.get("description") or record.get("summary")
            status = record.get("status") or ("observed" if kind in {"asset", "evidence"} else "candidate")
            nodes[node_id] = {
                "id": node_id,
                "kind": kind,
                "label": str(label)[:240],
                "description": str(description)[:1200] if description is not None else None,
                "status": str(status)[:40],
                "trust_state": str(record.get("trust_state") or "unverified")[:40],
                "evidence_ids": _ids(record.get("evidence_ids")),
            }

    edges: dict[str, dict[str, Any]] = {}

    def add_edge(subject: str, predicate: str, object_id: str, *,
                 edge_id: str | None = None, status: str = "observed",
                 trust_state: str = "unverified", evidence_ids: list[str] | None = None,
                 confidence: Any = None) -> None:
        if subject not in nodes or object_id not in nodes or subject == object_id:
            return
        key = edge_id or f"{predicate}:{subject}:{object_id}"
        if len(key) > 240:
            return
        try:
            numeric_confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            numeric_confidence = None
        if numeric_confidence is not None and not math.isfinite(numeric_confidence):
            numeric_confidence = None
        edges[key] = {
            "id": key,
            "subject_id": subject,
            "predicate": predicate[:80],
            "object_id": object_id,
            "status": status[:40],
            "trust_state": trust_state[:40],
            "evidence_ids": evidence_ids or [],
            "confidence": numeric_confidence,
        }

    for record in snapshot.get("relationships", []):
        if not isinstance(record, dict):
            continue
        subject, object_id = record.get("subject_id"), record.get("object_id")
        if not isinstance(subject, str) or not isinstance(object_id, str):
            continue
        add_edge(
            subject, str(record.get("predicate") or "related_to"), object_id,
            edge_id=str(record["id"]) if record.get("id") else None,
            status=str(record.get("status") or "candidate"),
            trust_state=str(record.get("trust_state") or "inferred"),
            evidence_ids=_ids(record.get("evidence_ids")),
            confidence=record.get("confidence"),
        )

    for asset in snapshot.get("assets", []):
        if not isinstance(asset, dict) or asset.get("id") not in nodes:
            continue
        asset_id = asset["id"]
        for evidence_id in _ids(asset.get("description_evidence_ids")):
            add_edge(asset_id, "supported_by", evidence_id)
        for evidence_id in _ids((asset.get("grain_contract") or {}).get("evidence_ids")):
            add_edge(asset_id, "supported_by", evidence_id)
        for field in asset.get("schema") or []:
            if not isinstance(field, dict) or field.get("description_status") != "approved":
                continue
            field_name = field.get("name")
            if not isinstance(field_name, str) or not field_name:
                continue
            field_id = stable_id("field", asset_id, field_name)
            nodes[field_id] = {
                "id": field_id, "kind": "field", "label": field_name[:240],
                "description": str(field.get("description") or "")[:1200],
                "status": "approved", "trust_state": "owner_reviewed",
                "evidence_ids": _ids(field.get("description_evidence_ids")),
            }
            add_edge(asset_id, "has_field", field_id, status="approved", trust_state="owner_reviewed")
            for evidence_id in _ids(field.get("description_evidence_ids")):
                add_edge(field_id, "supported_by", evidence_id, status="approved", trust_state="owner_reviewed")

    for collection in ("concepts", "metrics"):
        for record in snapshot.get(collection, []):
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            subject = record["id"]
            asset_ids = _ids([record.get("source_asset_id"), record.get("asset_id")])
            for provenance in record.get("provenance") or []:
                if isinstance(provenance, dict):
                    asset_ids.extend(_ids([provenance.get("asset_id")]))
            for asset_id in dict.fromkeys(asset_ids):
                add_edge(subject, "derived_from", asset_id)
            for evidence_id in _ids(record.get("evidence_ids")):
                add_edge(subject, "supported_by", evidence_id)

    for record in snapshot.get("evidence", []):
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            for asset_id in _ids([record.get("asset_id")]):
                add_edge(record["id"], "observed_in", asset_id)

    return list(nodes.values()), list(edges.values())


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and 0 < len(item) <= 160))[:40]


def slice_legacy_graph(
    snapshot: dict[str, Any], *, focus_id: str | None, depth: int, limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Serve pre-index snapshots until their next refresh writes indexed rows."""
    nodes, edges = project_graph(snapshot)
    by_id = {node["id"]: node for node in nodes}
    if focus_id:
        if focus_id not in by_id:
            raise KeyError(focus_id)
        selected = {focus_id: by_id[focus_id]}
    else:
        selected = {
            node["id"]: node for node in sorted(nodes, key=lambda item: (item["kind"], item["label"]))[:min(limit, 12)]
        }
    frontier = set(selected)
    chosen_edges: dict[str, dict[str, Any]] = {}
    for _ in range(depth):
        if not frontier or len(selected) >= limit:
            break
        candidates = [
            edge for edge in sorted(edges, key=lambda item: (item["predicate"], item["id"]))
            if edge["subject_id"] in frontier or edge["object_id"] in frontier
        ][:limit * 4]
        neighbors = []
        for edge in candidates:
            other = edge["object_id"] if edge["subject_id"] in frontier else edge["subject_id"]
            if other not in selected and other not in neighbors and len(selected) + len(neighbors) < limit:
                neighbors.append(other)
        for node_id in neighbors:
            selected[node_id] = by_id[node_id]
        frontier = set(neighbors)
        for edge in candidates:
            if edge["subject_id"] in selected and edge["object_id"] in selected:
                chosen_edges[edge["id"]] = edge
    if selected and not chosen_edges:
        for edge in edges:
            if edge["subject_id"] in selected and edge["object_id"] in selected:
                chosen_edges[edge["id"]] = edge
    return list(selected.values()), list(chosen_edges.values())[:limit * 2]
