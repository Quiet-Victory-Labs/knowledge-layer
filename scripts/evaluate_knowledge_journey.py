"""Connect two unrelated domains and grade candidate curation against held-out truth."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "tests/fixtures/curation/truth_manifest.json"
DEMO_NAMES = {
    "juniper": "Juniper Market sample",
    "northstar": "Northstar Field Service sample",
}


def _records(snapshot: dict[str, Any], collection: str) -> list[dict[str, Any]]:
    return [item for item in snapshot.get(collection, []) if isinstance(item, dict)]


def _field_concept_matches(
    snapshot: dict[str, Any], *, source_pointer: str, field: str, semantic_type: str
) -> bool:
    for concept in _records(snapshot, "concepts"):
        if concept.get("semantic_type") != semantic_type:
            continue
        for provenance in concept.get("provenance") or []:
            if not isinstance(provenance, dict):
                continue
            if str(provenance.get("source_pointer") or "").endswith(source_pointer) and str(
                provenance.get("field") or ""
            ) == field:
                return True
    return False


def _document_concept_matches(
    snapshot: dict[str, Any], *, source_pointer: str, term: str, semantic_type: str
) -> bool:
    for concept in _records(snapshot, "concepts"):
        if concept.get("semantic_type") != semantic_type:
            continue
        if str(concept.get("name") or "").casefold() != term.casefold():
            continue
        if any(
            isinstance(item, dict)
            and str(item.get("source_pointer") or "").endswith(source_pointer)
            for item in concept.get("provenance") or []
        ):
            return True
    return False


def _relationship_matches(snapshot: dict[str, Any], expected: dict[str, Any]) -> bool:
    assets = {item.get("id"): item for item in _records(snapshot, "assets")}
    for relation in _records(snapshot, "relationships"):
        source = assets.get(relation.get("source_asset_id"), {})
        target = assets.get(relation.get("target_asset_id"), {})
        if not str(source.get("source_pointer") or "").endswith(expected["source_asset"]):
            continue
        if not str(target.get("source_pointer") or "").endswith(expected["target_asset"]):
            continue
        if relation.get("source_field") != expected["source_field"]:
            continue
        if relation.get("target_field") != expected["target_field"]:
            continue
        if relation.get("status") != expected["expected_status"]:
            continue
        if expected.get("ambiguity") and relation.get("ambiguity") != expected["ambiguity"]:
            continue
        return True
    return False


def _grade(snapshot: dict[str, Any], truth: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, observed: Any = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "observed": observed})

    check("Discovery completed", snapshot.get("status") == "discovery_complete", snapshot.get("status"))
    check("Meaning remains uncertified", snapshot.get("meaning_certified") is False, snapshot.get("meaning_certified"))
    check("Agents are not marked ready", snapshot.get("agent_ready") is False, snapshot.get("agent_ready"))
    check("Assets discovered", bool(_records(snapshot, "assets")), len(_records(snapshot, "assets")))
    evidence_ids = {item.get("id") for item in _records(snapshot, "evidence")}
    unresolved = [
        item.get("id") for item in _records(snapshot, "proposals")
        if not item.get("evidence_ids")
        or not set(item.get("evidence_ids") or []).issubset(evidence_ids)
    ]
    check("Every proposal has resolvable evidence", not unresolved, unresolved[:5])

    for expected in truth["expected_concepts"]:
        if "field" in expected:
            found = _field_concept_matches(
                snapshot,
                source_pointer=expected["source_pointer"],
                field=expected["field"],
                semantic_type=expected["semantic_type"],
            )
            key = f"{expected['source_pointer']}:{expected['field']}"
        else:
            found = _document_concept_matches(
                snapshot,
                source_pointer=expected["source_pointer"],
                term=expected["term"],
                semantic_type=expected["semantic_type"],
            )
            key = f"{expected['source_pointer']}:{expected['term']}"
        check(f"Concept {key}", found)
    for expected in truth["expected_relationships"]:
        check(
            f"Candidate link {expected['source_asset']} -> {expected['target_asset']}",
            _relationship_matches(snapshot, expected),
        )
    if truth.get("deliberate_ambiguous_false_merge"):
        forbidden = [
            item.get("id") for item in _records(snapshot, "relationships")
            if item.get("status") in {"approved", "certified", "confirmed"}
            and item.get("source_field") == truth["deliberate_ambiguous_false_merge"]["field"]
        ]
        check("Reused identifier was not confirmed as one asset", not forbidden, forbidden)
    if truth.get("definition_conflict"):
        term = truth["definition_conflict"]["term"]
        concepts = {item.get("id"): item for item in _records(snapshot, "concepts")}
        conflicts = [
            item for item in _records(snapshot, "proposals")
            if item.get("proposal_type") == "definition_conflict"
            and term.casefold() in " ".join(
                str(value or "") for value in (
                    item.get("name"), item.get("term"), item.get("rationale"),
                    concepts.get(item.get("subject_id"), {}).get("name"),
                    concepts.get(item.get("object_id"), {}).get("name"),
                )
            ).casefold()
        ]
        check("Conflicting document definition is pending review", bool(conflicts) and all(item.get("status") in {"pending_review", "candidate"} for item in conflicts), len(conflicts))
    return checks


def evaluate(base_url: str) -> dict[str, Any]:
    manifest = json.loads(TRUTH.read_text(encoding="utf-8"))
    reports = []
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=60) as client:
        types = client.get("/api/knowledge/connector-types")
        types.raise_for_status()
        connector_names = {item["id"] for item in types.json()["connector_types"]}
        listed = client.get("/api/knowledge/connections")
        listed.raise_for_status()
        known = {item["name"]: item for item in listed.json()["connections"]}
        for domain in manifest["source_domains"]:
            domain_id = domain["tenant_id"]
            name = DEMO_NAMES[domain_id]
            if name in known:
                connection_id = known[name]["id"]
            else:
                created = client.post(
                    "/api/knowledge/connections",
                    json={"kind": "local_files", "name": name, "config": {"path": domain_id}},
                )
                created.raise_for_status()
                connection_id = created.json()["connection"]["id"]
            started = time.perf_counter()
            refreshed = client.post(f"/api/knowledge/connections/{connection_id}/refresh")
            elapsed = (time.perf_counter() - started) * 1000
            refreshed.raise_for_status()
            snapshot = refreshed.json()["snapshot"]
            checks = _grade(snapshot, domain)
            reports.append(
                {
                    "domain": domain_id,
                    "connection_id": connection_id,
                    "snapshot_id": snapshot.get("id"),
                    "snapshot_version": snapshot.get("version"),
                    "refresh_latency_ms": round(elapsed, 2),
                    "snapshot_bytes": len(refreshed.content),
                    "counts": {
                        kind: len(_records(snapshot, kind))
                        for kind in ("assets", "concepts", "relationships", "metrics", "proposals", "evidence")
                    },
                    "checks": checks,
                    "passed": all(item["passed"] for item in checks),
                }
            )
    return {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "connector_types_present": sorted(connector_names),
        "domains": reports,
        "passed": "local_files" in connector_names and all(item["passed"] for item in reports),
        "grading_scope": "Held-out expected facts and forbidden authoritative merges; not a full precision estimate.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/knowledge-journey-evaluation.json")
    args = parser.parse_args()
    report = evaluate(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"],
        "domains": [
            {
                "domain": item["domain"],
                "passed": item["passed"],
                "checks": len(item["checks"]),
                "refresh_latency_ms": item["refresh_latency_ms"],
            }
            for item in report["domains"]
        ],
        "report": str(args.output),
    }, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
