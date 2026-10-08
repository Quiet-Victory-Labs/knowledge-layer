"""Small, source-scoped search records derived from current metadata snapshots."""

from __future__ import annotations

from typing import Any


def records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Index only bounded metadata and evidence handles, never source data."""
    evidence = {
        str(row.get("id")): row
        for row in snapshot.get("evidence", [])
        if isinstance(row, dict) and row.get("id")
    }
    result = []
    for concept in snapshot.get("concepts", []):
        if not isinstance(concept, dict) or not concept.get("id"):
            continue
        evidence_ids = [str(item) for item in concept.get("evidence_ids") or []][:5]
        evidence_summary = " ".join(
            str(evidence[item].get("summary") or "") for item in evidence_ids if item in evidence
        )[:600]
        evidence_handles = [
            {"id": item, "summary": str(evidence[item].get("summary") or "")[:240],
             "source_pointer": evidence[item].get("source_pointer")}
            for item in evidence_ids[:2] if item in evidence
        ]
        text = " ".join(
            str(concept.get(field) or "")
            for field in ("name", "definition", "description", "semantic_type")
        )
        text = f"{text} {evidence_summary}".strip()[:1000]
        result.append({
            "concept_id": str(concept["id"]),
            "name": str(concept.get("name") or "")[:240],
            "definition": str(concept.get("definition") or "")[:320] or None,
            "semantic_type": str(concept.get("semantic_type") or "")[:80] or None,
            "trust_state": str(concept.get("trust_state") or concept.get("status") or "")[:40] or None,
            "evidence_ids": evidence_ids,
            "evidence": evidence_handles,
            "text": text,
        })
    return result
