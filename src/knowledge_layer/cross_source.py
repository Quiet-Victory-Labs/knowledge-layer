"""Reviewed cross-source joins, pinned to authorized source evidence.

This extends the contract proposal ledger; it neither matches names nor queries
source rows. Namespace and cardinality declarations require independent review.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from knowledge_layer.store import KnowledgeConflict, KnowledgeValidationError

_CHECKING: ContextVar[tuple[tuple[str, str], ...]] = ContextVar("join_dependencies", default=())
MAX_DEPENDENCY_PROJECTIONS = 64
_PROJECTIONS: ContextVar[dict[str, int] | None] = ContextVar("dependency_projection_budget", default=None)
STRUCTURED_EVIDENCE = frozenset({"asset_metadata", "schema_and_profile"})
_CARDINALITIES = frozenset({"one_to_one", "many_to_one", "one_to_many", "many_to_many"})
_KINDS = frozenset({"identity_equivalence", "business_join"})
_INPUT_KEYS = frozenset({
    "to_connection_id", "to_snapshot_id", "to_asset_id", "from_columns", "to_columns",
    "cardinality", "caveat", "relationship_kind", "from_namespace", "to_namespace",
})


@contextmanager
def dependency_projection():
    """Bound total traversal work as well as depth, including branching graphs.

    The budget is confined to one outer snapshot read and its dependencies.
    Exceeding it refuses the read; it cannot return a partial approved graph.
    """
    state = _PROJECTIONS.get()
    token = None
    if state is None:
        state = {"count": 0}
        token = _PROJECTIONS.set(state)
    try:
        state["count"] += 1
        if state["count"] > MAX_DEPENDENCY_PROJECTIONS:
            raise KnowledgeConflict("Dependency traversal exceeds 64 snapshot projections; narrow the relationship scope")
        from knowledge_layer.inventory_citations import inventory_citation_operation

        with inventory_citation_operation():
            yield
    finally:
        if token is not None:
            _PROJECTIONS.reset(token)


def _text(value: Any, label: str, maximum: int = 255) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise KnowledgeValidationError(f"{label} must be a bounded nonempty string")
    return value


def _asset(snapshot: dict[str, Any], asset_id: str) -> dict[str, Any]:
    matches = [item for item in snapshot.get("assets", [])
               if isinstance(item, dict) and item.get("id") == asset_id]
    if len(matches) != 1 or matches[0].get("asset_type") != "table":
        raise KnowledgeValidationError("Join must bind an exact current table asset")
    return matches[0]


def asset_binding(asset: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    """Pin actual metadata; unknown native fields remain null."""
    if (not isinstance(columns, list) or not 1 <= len(columns) <= 8
            or any(not isinstance(name, str) for name in columns)
            or len(set(columns)) != len(columns)):
        raise KnowledgeValidationError("Join columns must contain 1-8 distinct names")
    fields = []
    for name in columns:
        matches = [field for field in asset.get("schema", [])
                   if isinstance(field, dict) and field.get("name") == name]
        if len(matches) != 1:
            raise KnowledgeValidationError("Join field must identify one current source field")
        field = matches[0]
        fields.append({key: field.get(key) for key in
                       ("name", "data_type", "native_type", "field_id", "nullable")})
    facts = {"asset_id": asset["id"], "source_pointer": asset.get("source_pointer"),
             "source_revision": asset.get("revision"), "fields": fields}
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return {**facts, "metadata_digest": hashlib.sha256(encoded).hexdigest()}


def _citations(snapshot: dict[str, Any], asset_id: str, ids: list[str]) -> None:
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 12
            or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids)):
        raise KnowledgeValidationError("Join requires distinct observed evidence handles")
    evidence = {item.get("id"): item for item in snapshot.get("evidence", [])
                if isinstance(item, dict)}
    if not set(ids).issubset(evidence) or not any(
        evidence[item].get("asset_id") == asset_id
        and evidence[item].get("evidence_type") in STRUCTURED_EVIDENCE for item in ids
    ):
        raise KnowledgeValidationError("Join must cite observed metadata from its table")


def validate_cross_source_join(
    store: Any, *, tenant_id: str, connection_id: str, snapshot: dict[str, Any],
    asset_id: str, payload: dict[str, Any], evidence_ids: list[str],
    evidence_refs: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _INPUT_KEYS:
        raise KnowledgeValidationError("Cross-source join requires its complete typed contract")
    for key in ("to_connection_id", "to_snapshot_id", "to_asset_id", "from_namespace", "to_namespace"):
        _text(payload[key], key)
    _text(payload["caveat"], "caveat", 1000)
    if payload["to_connection_id"] == connection_id:
        raise KnowledgeValidationError("Use join_rule for a same-source relationship")
    if (not isinstance(payload["cardinality"], str) or not isinstance(payload["relationship_kind"], str)
            or payload["cardinality"] not in _CARDINALITIES or payload["relationship_kind"] not in _KINDS):
        raise KnowledgeValidationError("Join kind and cardinality must be explicit supported values")
    if (payload["relationship_kind"] == "identity_equivalence"
            and payload["cardinality"] != "one_to_one"):
        raise KnowledgeValidationError("Identity equivalence requires a one-to-one reviewed bridge")
    target = store.get_connection(tenant_id, payload["to_connection_id"])
    if target is None:
        raise KnowledgeConflict("Join dependency is unavailable")
    other = target.get("snapshot") or {}
    if (target.get("status") == "error" or other.get("id") != payload["to_snapshot_id"]
            or other.get("agent_ready") is not True):
        raise KnowledgeConflict("Join dependency needs a current published snapshot")
    source_asset = _asset(snapshot, asset_id)
    target_asset = _asset(other, payload["to_asset_id"])
    left = asset_binding(source_asset, payload["from_columns"])
    right = asset_binding(target_asset, payload["to_columns"])
    if len(left["fields"]) != len(right["fields"]):
        raise KnowledgeValidationError("Join column pairs must align")
    # Broad type compatibility does not certify provider coercion or identity.
    for source_field, target_field in zip(left["fields"], right["fields"], strict=True):
        source_type, target_type = source_field["data_type"], target_field["data_type"]
        if (source_type is None or target_type is None or source_type != target_type
                or source_type in {"structured", "unknown"}):
            raise KnowledgeValidationError("Join field types must be known compatible scalars")
    _citations(snapshot, asset_id, evidence_ids)
    refs = [ref for ref in evidence_refs
            if ref.get("connection_id") == payload["to_connection_id"]
            and ref.get("snapshot_id") == payload["to_snapshot_id"]]
    _citations(other, payload["to_asset_id"], [ref["evidence_id"] for ref in refs])
    return {**payload, "source_binding": {"connection_id": connection_id,
            "observed_snapshot_id": snapshot["id"], **left},
            "target_binding": {"connection_id": payload["to_connection_id"],
            "snapshot_id": other["id"], "snapshot_version": other["version"], **right},
            "coverage": {"state": "not_measured", "source_rows": None,
                         "target_rows": None, "matched_rows": None},
            "execution_state": "not_executed"}


def structured_reference_current(store: Any, tenant_id: str, ref: dict[str, str]) -> bool:
    """Revalidate effective publication, including nested dependencies, boundedly."""
    key = (tenant_id, ref.get("connection_id", ""))
    checking = _CHECKING.get()
    if key in checking or len(checking) >= 8:
        return False
    token = _CHECKING.set((*checking, key))
    try:
        source = store.get_connection(tenant_id, key[1])
        snapshot = (source or {}).get("snapshot") or {}
        if ((source or {}).get("status") == "error" or snapshot.get("id") != ref.get("snapshot_id")
                or snapshot.get("agent_ready") is not True):
            return False
        return any(isinstance(item, dict) and item.get("id") == ref.get("evidence_id")
                   and item.get("evidence_type") in STRUCTURED_EVIDENCE
                   for item in snapshot.get("evidence", []))
    finally:
        _CHECKING.reset(token)


def source_binding_current(snapshot: dict[str, Any], payload: dict[str, Any]) -> bool:
    try:
        current = asset_binding(_asset(snapshot, payload["source_binding"]["asset_id"]),
                                payload["from_columns"])
        return current["metadata_digest"] == payload["source_binding"]["metadata_digest"]
    except (KeyError, TypeError, KnowledgeValidationError):
        return False


def target_binding_current(store: Any, tenant_id: str, payload: dict[str, Any]) -> bool:
    try:
        other = store.get_snapshot(tenant_id, payload["to_connection_id"]) or {}
        if other.get("id") != payload["to_snapshot_id"]:
            return False
        current = asset_binding(_asset(other, payload["to_asset_id"]), payload["to_columns"])
        return current["metadata_digest"] == payload["target_binding"]["metadata_digest"]
    except (KeyError, TypeError, KnowledgeValidationError):
        return False


def relationship_plan(store: Any, *, tenant_id: str, connection_id: str,
                      assertion_id: str) -> dict[str, Any]:
    from knowledge_layer.contracts import semantic_contract
    from knowledge_layer.store import KnowledgeNotFound

    contract = semantic_contract(store, tenant_id=tenant_id, connection_id=connection_id)
    matches = [item for item in contract.get("cross_source_joins", [])
               if item.get("decision_proposal_id") == assertion_id]
    if len(matches) != 1 or not contract["agent_ready"]:
        raise KnowledgeNotFound("Current published relationship was not found")
    relation = matches[0]
    fanout = relation["cardinality"] in {"one_to_many", "many_to_many"}
    return {"status": "blocked" if fanout else "metadata_plan",
            "blocked_reasons": ["fanout_requires_explicit_result_grain"] if fanout else [],
            "relationship": relation, "execution_state": "not_executed",
            "executor": "customer_harness", "required_checks": [
                "current_source_schema", "source_native_permissions", "identity_namespaces",
                "key_uniqueness", "join_coverage", "declared_cardinality", "result_grain"],
            "source_queries": "separate_authorized_reads", "result_state": "not_computed"}
