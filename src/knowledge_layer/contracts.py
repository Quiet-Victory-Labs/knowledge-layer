"""Evidence-bound semantic contract drafts for agent curation.

The internal contract keeps source bindings and review state separate from an
interchange format. Apache Ossie is a projection of approved, queryable parts.
"""

from __future__ import annotations

import json
import re
from bisect import insort
from importlib.resources import files
from typing import Any

from knowledge_layer.connectors.common import normalize_name, stable_id
from knowledge_layer.inventory_citations import inventory_citation_operation
from knowledge_layer.store import KnowledgeValidationError

CONTRACT_KINDS = frozenset({
    "dataset_description", "field_description", "grain", "join_rule", "cross_source_join", "metric", "query_example",
    "term_binding", "tenant_boundary",
})
TERM_BINDING_RELATIONSHIPS = frozenset({"represents", "measures", "mentions", "related_to"})
_AGGREGATIONS = frozenset({"sum", "avg", "min", "max", "count", "count_distinct", "composite"})
_CARDINALITIES = frozenset({"many_to_one", "one_to_one", "one_to_many", "many_to_many"})
_DIALECTS = frozenset({"ANSI_SQL", "SNOWFLAKE", "BIGQUERY", "DATABRICKS"})


def _text(value: Any, label: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or not 4 <= len(value.strip()) <= limit:
        raise KnowledgeValidationError(f"{label} must be 4-{limit} characters")
    return value.strip()


def _fields(asset: dict[str, Any]) -> set[str]:
    return {str(field.get("name")) for field in asset.get("schema", []) if isinstance(field, dict) and field.get("name")}


def _field_list(value: Any, asset: dict[str, Any], label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8 or any(not isinstance(item, str) for item in value):
        raise KnowledgeValidationError(f"{label} must contain 1-8 field names")
    if len(set(value)) != len(value) or not set(value).issubset(_fields(asset)):
        raise KnowledgeValidationError(f"{label} must identify distinct current source fields")
    return value


def validate_assertion(
    snapshot: dict[str, Any], *, kind: str, asset_id: str, payload: dict[str, Any],
    evidence_ids: list[str], source_kind: str,
) -> dict[str, Any]:
    """Validate a typed draft against the *current* source snapshot."""
    assets = {item["id"]: item for item in snapshot.get("assets", []) if isinstance(item, dict) and item.get("id")}
    evidence = {item["id"]: item for item in snapshot.get("evidence", []) if isinstance(item, dict) and item.get("id")}
    if kind not in CONTRACT_KINDS or asset_id not in assets or not isinstance(payload, dict):
        raise KnowledgeValidationError("Unknown contract kind, asset, or payload")
    if kind == "cross_source_join":
        raise KnowledgeValidationError("Cross-source joins require registry dependency validation")
    if (not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 12
            or any(not isinstance(item, str) for item in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)):
        raise KnowledgeValidationError("Cite 1-12 distinct evidence handles")
    if not set(evidence_ids).issubset(evidence):
        raise KnowledgeValidationError("Evidence must belong to the current source snapshot")
    asset = assets[asset_id]
    expected: set[str]
    if kind == "dataset_description":
        expected = {"description"}
        result = {"description": _text(payload.get("description"), "description", 2000)}
    elif kind == "field_description":
        expected = {"field", "description"}
        field = payload.get("field")
        if asset.get("asset_type") != "table" or field not in _fields(asset):
            raise KnowledgeValidationError("Field must belong to the selected current table")
        result = {"field": field, "description": _text(payload.get("description"), "description", 2000)}
    elif kind == "term_binding":
        expected = {"target_connection_id", "target_snapshot_id", "target_concept_id", "relationship"}
        if "field" in payload:
            expected.add("field")
        if asset.get("asset_type") not in {"table", "document", "semantic_definition"}:
            raise KnowledgeValidationError("A term binding requires a current table, document, or semantic definition")
        result = {}
        for key in ("target_connection_id", "target_snapshot_id", "target_concept_id"):
            value = payload.get(key)
            if (not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise KnowledgeValidationError(f"{key} must identify an exact current source record")
            result[key] = value
        relationship = payload.get("relationship")
        if not isinstance(relationship, str) or relationship not in TERM_BINDING_RELATIONSHIPS:
            raise KnowledgeValidationError("Term relationship must be represents, measures, mentions, or related_to")
        result["relationship"] = relationship
        if "field" in payload:
            field = payload["field"]
            if (asset.get("asset_type") != "table" or not isinstance(field, str)
                    or sum(1 for item in asset.get("schema", [])
                           if isinstance(item, dict) and item.get("name") == field) != 1):
                raise KnowledgeValidationError("Term binding field must identify one observed source table field")
            result["field"] = field
        metadata = [evidence[item] for item in evidence_ids if evidence[item].get("asset_id") == asset_id]
        if "field" in result:
            supported = any(item.get("evidence_type") == "schema_and_profile"
                            and (item.get("details") or {}).get("field") == result["field"] for item in metadata)
        else:
            supported = any(item.get("evidence_type") == "asset_metadata" for item in metadata)
        if not supported:
            raise KnowledgeValidationError("Term binding must cite observed metadata for its exact source asset or field")
    elif kind == "grain":
        expected = {"description", "key_fields"}
        if "required_partition_filter" in payload:
            expected.add("required_partition_filter")
        if "partition_keys" in payload:
            expected.add("partition_keys")
        if asset.get("asset_type") != "table":
            raise KnowledgeValidationError("Grain requires a table asset")
        result = {
            "description": _text(payload.get("description"), "grain description", 1000),
            "key_fields": _field_list(payload.get("key_fields"), asset, "key_fields"),
        }
        if "required_partition_filter" in payload:
            result["required_partition_filter"] = _text(payload["required_partition_filter"], "required partition filter", 500)
        if "partition_keys" in payload:
            result["partition_keys"] = _field_list(payload["partition_keys"], asset, "partition_keys")
    elif kind == "tenant_boundary":
        expected = {"description", "isolated_entity_types"}
        if "prohibited_cross_joins" in payload:
            expected.add("prohibited_cross_joins")
        result = {
            "description": _text(payload.get("description"), "tenant boundary description", 1000),
            "isolated_entity_types": _text(payload.get("isolated_entity_types"), "isolated entity types", 1000),
        }
        if "prohibited_cross_joins" in payload:
            result["prohibited_cross_joins"] = _text(payload.get("prohibited_cross_joins"), "prohibited cross joins", 1000)
    elif kind == "join_rule":
        expected = {"to_asset_id", "from_columns", "to_columns", "cardinality", "caveat"}
        target = assets.get(payload.get("to_asset_id"))
        if asset.get("asset_type") != "table" or not target or target.get("asset_type") != "table" or target["id"] == asset_id:
            raise KnowledgeValidationError("Join must connect two distinct current table assets")
        from_columns = _field_list(payload.get("from_columns"), asset, "from_columns")
        to_columns = _field_list(payload.get("to_columns"), target, "to_columns")
        if len(from_columns) != len(to_columns) or payload.get("cardinality") not in _CARDINALITIES:
            raise KnowledgeValidationError("Join columns must align and cardinality must be explicit")
        if not {asset_id, target["id"]}.issubset({evidence[item].get("asset_id") for item in evidence_ids}):
            raise KnowledgeValidationError("A join must cite evidence from both source assets")
        result = {
            "to_asset_id": target["id"], "from_columns": from_columns, "to_columns": to_columns,
            "cardinality": payload["cardinality"],
            "caveat": _text(payload.get("caveat"), "join caveat", 1000),
        }
    elif kind == "metric":
        from knowledge_layer.metric_semantics import validate_metric_payload

        if asset.get("asset_type") != "table":
            raise KnowledgeValidationError("Metric requires a table asset")
        result = validate_metric_payload(payload, asset.get("schema") or [])
        expected = set(payload)  # The metric validator owns both exact versioned shapes.
    else:
        expected = {"question", "dialect", "sql", "expected_grain"}
        if source_kind not in {"postgres", "sql_catalog"} or asset.get("asset_type") != "table":
            raise KnowledgeValidationError("SQL examples require a queryable SQL source")
        sql = _text(payload.get("sql"), "SQL example", 4000)
        if payload.get("dialect") not in _DIALECTS or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
            raise KnowledgeValidationError("SQL example needs a supported dialect and SELECT statement")
        if re.search(r";|--|/\*|\*/|\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|COPY|CALL|EXEC|GRANT|REVOKE)\b", sql, re.IGNORECASE):
            raise KnowledgeValidationError("SQL example must be one read-only statement")
        result = {
            "question": _text(payload.get("question"), "question", 500),
            "dialect": payload["dialect"], "sql": sql,
            "expected_grain": _text(payload.get("expected_grain"), "expected grain", 500),
            "execution_verified": False,
        }
    if set(payload) != expected:
        raise KnowledgeValidationError(f"{kind} payload fields must be {sorted(expected)}")
    if not any(evidence[item].get("asset_id") == asset_id for item in evidence_ids):
        raise KnowledgeValidationError("Cite at least one evidence handle for the target asset")
    if kind == "field_description" and not any(
        evidence[item].get("asset_id") == asset_id
        and (evidence[item].get("details") or {}).get("field") == result["field"]
        for item in evidence_ids
    ):
        raise KnowledgeValidationError("A field description must cite its observed field evidence")
    return result


def validate_term_binding_target(
    snapshot: dict[str, Any], payload: dict[str, Any], evidence_refs: list[dict[str, str]],
) -> dict[str, Any]:
    """Bind one exact document/native term citation without equating namespaces."""
    if (len(evidence_refs) != 1 or set(evidence_refs[0]) != {"connection_id", "snapshot_id", "evidence_id"}
            or evidence_refs[0]["connection_id"] != payload["target_connection_id"]
            or evidence_refs[0]["snapshot_id"] != payload["target_snapshot_id"]
            or snapshot.get("id") != payload["target_snapshot_id"]):
        raise KnowledgeValidationError("Term binding requires one exact current target concept evidence_ref")
    concepts = [item for item in snapshot.get("concepts", []) if isinstance(item, dict)
                and item.get("id") == payload["target_concept_id"]]
    ref = evidence_refs[0]
    evidence = next((item for item in snapshot.get("evidence", []) if isinstance(item, dict)
                     and item.get("id") == ref["evidence_id"]), None)
    if len(concepts) != 1 or evidence is None or ref["evidence_id"] not in (concepts[0].get("evidence_ids") or []):
        raise KnowledgeValidationError("Target evidence must support the exact current target concept")
    concept = concepts[0]
    asset = next((item for item in snapshot.get("assets", []) if isinstance(item, dict)
                  and item.get("id") == evidence.get("asset_id")), None)
    if asset is None:
        raise KnowledgeValidationError("Target concept must have a current source asset")
    if evidence.get("evidence_type") == "document_derived_term":
        provenance = concept.get("provenance") or []
        if (concept.get("semantic_type") != "document_business_term" or asset.get("asset_type") != "document"
                or not any(isinstance(item, dict) and item.get("asset_id") == asset["id"] for item in provenance)):
            raise KnowledgeValidationError("Target must be the exact source-local document term")
    elif evidence.get("evidence_type") == "upstream_semantic_definition":
        from knowledge_layer.federated import _upstream_asset

        native_asset = _upstream_asset(snapshot, evidence)
        native = (native_asset or {}).get("upstream_definition") or {}
        binding = concept.get("source_binding") or {}
        if (not native_asset or concept.get("id") != stable_id("concept", asset["id"], "upstream_definition")
                or concept.get("semantic_type") != "upstream_" + str(native.get("kind"))
                or concept.get("upstream_definition") != native
                or binding.get("asset_id") != asset["id"]
                or binding.get("source_pointer") != asset.get("source_pointer")):
            raise KnowledgeValidationError("Target must be the exact imported native concept binding")
    else:
        raise KnowledgeValidationError("Term bindings support document terms and imported native definitions")
    return concept


def reviewed_term_binding_target(
    snapshot: dict[str, Any], payload: dict[str, Any], evidence_refs: list[dict[str, str]],
) -> bool:
    """Require a current local definition review anchored to the cited term evidence."""
    try:
        concept = validate_term_binding_target(snapshot, payload, evidence_refs)
    except (KnowledgeValidationError, KeyError, TypeError):
        return False
    handle = evidence_refs[0]["evidence_id"]
    handles = {item.get("id") for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    review = next((item for item in snapshot.get("proposals", []) if isinstance(item, dict)
                   and item.get("id") == concept.get("definition_proposal_id")), {})
    return bool(
        concept.get("definition_status") == "approved" and concept.get("definition")
        and handle in (concept.get("definition_evidence_ids") or [])
        and set(concept.get("definition_evidence_ids") or []).issubset(handles)
        and review.get("proposal_type") == "semantic_correction" and review.get("status") == "approved"
        and review.get("effective_on_current_snapshot") is True
        and review.get("target_concept_id") == concept["id"]
        and review.get("proposed_definition") == concept["definition"]
        and handle in (review.get("evidence_ids") or [])
    )


def list_contract_work(store: Any, *, tenant_id: str, connection_id: str, limit: int = 5, offset: int = 0) -> dict[str, Any]:
    """Offer bounded table/field/grain/join/metric work, with document context."""
    if not 1 <= limit <= 10 or not 0 <= offset < 500:
        raise ValueError("limit must be 1-10 and offset must be 0-499")
    connection = store.get_connection(tenant_id, connection_id)
    if not connection:
        raise ValueError("Connection was not found")
    snapshot = connection.get("snapshot") or {}
    assets = {item["id"]: item for item in snapshot.get("assets", []) if isinstance(item, dict) and item.get("id")}
    evidence = {item["id"]: item for item in snapshot.get("evidence", []) if isinstance(item, dict) and item.get("id")}
    def key(kind: Any, asset_id: Any, detail: dict[str, Any]) -> tuple[Any, ...]:
        if kind == "field_description":
            return kind, asset_id, detail.get("field")
        if kind == "metric":
            return kind, asset_id, detail.get("measure_field") or detail.get("field")
        if kind == "join_rule":
            return kind, asset_id, detail.get("to_asset_id"), tuple(detail.get("from_columns") or [])
        return kind, asset_id

    submitted = {
        key(item.get("assertion_kind"), item.get("asset_id"), item.get("payload") or {})
        for item in snapshot.get("proposals", []) if isinstance(item, dict)
        and item.get("proposal_type") == "contract_assertion" and item.get("status") in {"pending_review", "approved"}
    }
    tasks: list[dict[str, Any]] = []

    def add(kind: str, asset: dict[str, Any], details: dict[str, Any], handles: list[str], priority: int) -> None:
        if key(kind, asset["id"], details) in submitted or not handles or len(tasks) >= 500:
            return
        tasks.append({
            "work_id": stable_id("contract_work", snapshot.get("id"), kind, asset["id"], details),
            "kind": kind, "priority": priority, "connection_id": connection_id,
            "snapshot_id": snapshot.get("id"), "asset_id": asset["id"], "asset_name": asset.get("name"),
            "source_pointer": asset.get("source_pointer"), "candidate": details,
            "evidence_ids": handles[:4],
            "evidence": [{"id": key, "summary": str(evidence[key].get("summary") or "")[:320],
                          "source_pointer": evidence[key].get("source_pointer")}
                         for key in handles[:4] if key in evidence],
            "submission_tool": "propose_contract_assertion",
        })

    doc_evidence = [item for item in evidence.values() if item.get("evidence_type") == "document_derived_term"]
    for asset in assets.values():
        base = [item["id"] for item in evidence.values() if item.get("asset_id") == asset["id"] and item.get("evidence_type") == "asset_metadata"][:1]
        if not base:
            continue
        if asset.get("asset_type") == "document":
            add("dataset_description", asset, {"asset_type": "document"}, base, 35)
            continue
        if asset.get("asset_type") != "table":
            continue
        add("dataset_description", asset, {"asset_type": "table", "field_count": len(asset.get("schema", []))}, base, 85)
        key_candidates = [field.get("name") for field in asset.get("schema", [])
                          if str(field.get("name", "")).endswith(("_id", "_key"))
                          or field.get("name") in ((asset.get("profile") or {}).get("primary_key") or [])]
        if key_candidates:
            add("grain", asset, {"candidate_key_fields": key_candidates[:8], "profile": {
                "row_count": (asset.get("profile") or {}).get("row_count"),
                "profile_rows": (asset.get("profile") or {}).get("profile_rows"),
                "row_count_is_complete": (asset.get("profile") or {}).get("row_count_is_complete"),
                "null_counts": {key: (asset.get("profile") or {}).get("null_counts", {}).get(key)
                                for key in key_candidates[:8]},
                "distinct_counts": {key: (asset.get("profile") or {}).get("distinct_counts", {}).get(key)
                                    for key in key_candidates[:8]},
            }}, base, 80)
        for field in asset.get("schema", []):
            name = field.get("name")
            field_handles = [item["id"] for item in evidence.values() if item.get("asset_id") == asset["id"] and (item.get("details") or {}).get("field") == name][:1]
            words = set(normalize_name(str(name)).split()) - {"id", "key", "code", "date", "at"}
            related_docs = [item["id"] for item in doc_evidence if words and words.intersection(set(normalize_name(str(item.get("summary"))).split()))][:2]
            add("field_description", asset, {"field": name, "data_type": field.get("data_type"),
                "nullable": field.get("nullable")}, field_handles + related_docs, 75 if related_docs else 45)
    for relation in snapshot.get("relationships", []):
        if not isinstance(relation, dict) or not relation.get("source_asset_id") or not relation.get("target_asset_id"):
            continue
        asset = assets.get(relation["source_asset_id"])
        if asset and relation.get("source_field") and relation.get("target_field"):
            add("join_rule", asset, {"to_asset_id": relation["target_asset_id"],
                "to_asset_name": assets.get(relation["target_asset_id"], {}).get("name"),
                "from_columns": [part.strip() for part in relation["source_field"].split(",")],
                "to_columns": [part.strip() for part in relation["target_field"].split(",")],
                "candidate_relation_status": relation.get("status"),
                "join_probe": relation.get("join_probe"),
                "caveat": relation.get("rationale")}, list(relation.get("evidence_ids") or []), 65)
    for metric in snapshot.get("metrics", []):
        if isinstance(metric, dict) and (asset := assets.get(metric.get("source_asset_id"))):
            add("metric", asset, {"field": metric.get("field"), "unit_candidate": metric.get("unit"),
                "grain_candidate": metric.get("grain"), "metric_id": metric.get("id")},
                list(metric.get("evidence_ids") or []), 55)
    tasks.sort(key=lambda item: (-item["priority"], str(item["asset_name"]), item["work_id"]))
    return {"connection_id": connection_id, "snapshot_id": snapshot.get("id"),
            "work": tasks[offset:offset + limit], "total": len(tasks), "offset": offset,
            "more_available": len(tasks) > offset + limit,
            "trust_policy": "Metadata and documents are evidence, not authority. Submit only supported drafts; owner review is required."}


def _metric_query_guidance(
    snapshot: dict[str, Any], *, connection: dict[str, Any], connection_id: str, asset: dict[str, Any],
    field: dict[str, Any], metric: dict[str, Any], proposal: dict[str, Any],
) -> dict[str, Any]:
    """Plan from reviewed metadata without computing values or supplying SQL."""
    counting = metric["aggregation"] in {"count", "count_distinct"}
    numeric = field.get("data_type") in {"integer", "number"}
    guidance = {
        "status": "metadata_guidance",
        "source_binding": {
            "connection_id": connection_id, "snapshot_id": snapshot.get("id"),
            "snapshot_version": snapshot.get("version"), "asset_id": asset["id"],
            "source_pointer": asset.get("source_pointer"), "field": field["name"],
            "data_type": field.get("data_type"), "native_type": field.get("native_type"),
            "field_id": field.get("field_id"), "nullable": field.get("nullable"),
        },
        "source_state": {
            "connection_status": connection.get("status"), "last_error": connection.get("last_error"),
            "last_refreshed_at": connection.get("last_refreshed_at"),
            "agent_ready": snapshot.get("agent_ready") is True, "current_schema_verified": False,
            "freshness_state": "source_unavailable" if connection.get("status") == "error" else "not_live_verified",
        },
        "approval": {
            "assertion_id": proposal["id"], "source_snapshot_id": proposal.get("source_snapshot_id"),
            "status": "approved", "effective_on_current_snapshot": True,
            "evidence_ids": proposal["evidence_ids"], "evidence_refs": proposal.get("evidence_refs") or [],
        },
        "value_policy": {
            "null_measure": "missing", "numeric_zero_measure": "observed_value",
            "replace_missing_with_zero": False, "missing_source_field": "refuse",
        },
        "aggregation_policy": {
            "aggregation": metric["aggregation"], "input_values": "non_null_measure_values",
            "result_when_no_known_values": "zero_count" if counting else "null",
            "zero_count_proves_zero_measure_or_spend": False,
        },
        "diagnostics": {
            "required_counts": ["selected_rows", "known_measure_rows", "missing_measure_rows"]
            + (["zero_measure_rows"] if numeric else []),
            "zero_measure_count_applicable": numeric,
            "counts_execution_state": "not_computed",
        },
        "comparison_policy": {
            "applies_to": "paired_measure_comparison",
            "same_row_cohort_required": True, "paired_non_null_filter_required": True,
            "preserve_observed_zero_values": True,
            "joins": "only_explicit_approved_join_contracts",
        },
        "execution": {
            "executor": "customer_harness", "execution_verified": False,
            "result_state": "not_computed", "sql_examples_status": "unexecuted",
            "preserve_declared_cohorts": True,
            "required_checks": ["latest_source_schema", "provider_native_type_and_aggregation", "scope_and_filters", "grain_and_cardinality"],
        },
        "warning": "Guidance is derived from approved metadata, not executed results. "
        "The customer harness must execute and verify provider-specific SQL; SQL examples remain unexecuted.",
    }

    semantics = metric.get("semantics") or {}
    if semantics.get("schema_version") in {"metric-v2", "metric-composite-v1"}:
        guidance["metric_semantics"] = semantics
        guidance["planning_tool"] = "plan_metric_query"
        guidance["aggregation_policy"] = {
            "aggregation": metric["aggregation"], "input_values": "reviewed_expression_and_filters",
            "cohort": semantics["cohort"], "ratio": semantics.get("ratio"),
            "additivity": semantics["additivity"], "time": semantics["time"],
            "required_policy": "Use the typed planner; do not reduce this definition to the legacy measure field.",
        }
    return guidance


@inventory_citation_operation()
def semantic_contract(store: Any, *, tenant_id: str, connection_id: str) -> dict[str, Any]:
    """Return current source bindings, accepted assertions, and visible candidates."""
    from knowledge_layer.federated import (
        cited_glossary,
        cited_inventory_evidence,
        cited_upstream_definitions,
    )

    connection = store.get_connection(tenant_id, connection_id)
    if not connection:
        raise ValueError("Connection was not found")
    snapshot = connection.get("snapshot") or {}
    assets = [item for item in snapshot.get("assets", []) if isinstance(item, dict)]
    assets_by_id = {item["id"]: item for item in assets if item.get("id")}
    current_assets = set(assets_by_id)
    upstream_definitions = []
    for concept in snapshot.get("concepts", []):
        if not isinstance(concept, dict) or not isinstance(concept.get("upstream_definition"), dict):
            continue
        binding = concept.get("source_binding") or {}
        if binding.get("asset_id") not in current_assets:
            continue
        upstream_definitions.append({
            **concept["upstream_definition"], "asset_id": binding["asset_id"], "concept_id": concept.get("id"),
            "source_pointer": binding.get("source_pointer"), "evidence_ids": concept.get("evidence_ids") or [],
            "source_revision": assets_by_id[binding["asset_id"]].get("revision"),
            "status": "observed_upstream", "definition_status": "upstream_declared",
            "trust_state": "observed_upstream_definition", "executable": False, "certified": False,
            "kl_definition_status": concept.get("definition_status") or "candidate",
            "kl_definition": concept.get("definition") if concept.get("definition_status") == "approved" else None,
        })
    assertions = [item for item in snapshot.get("proposals", []) if isinstance(item, dict)
                  and item.get("proposal_type") == "contract_assertion"
                  and item.get("asset_id") in current_assets]
    metric_assertions = {item["id"]: item for item in assertions if item.get("id")
                         and item.get("assertion_kind") == "metric" and item.get("status") == "approved"
                         and item.get("effective_on_current_snapshot") is True}
    current_evidence = {item.get("id") for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    metrics = []
    for metric in snapshot.get("metrics", []):
        if not isinstance(metric, dict) or metric.get("status") != "approved":
            continue
        proposal = metric_assertions.get(metric.get("decision_proposal_id"))
        asset = next((item for item in assets if item.get("id") == metric.get("source_asset_id")), None)
        if not proposal or not asset or asset.get("asset_type") != "table":
            continue
        fields = [item for item in asset.get("schema", []) if isinstance(item, dict) and item.get("name") == metric.get("field")]
        payload = proposal.get("payload") or {}
        handles = proposal.get("evidence_ids") or []
        if (len(fields) != 1 or proposal.get("asset_id") != asset["id"]
                or not handles or not set(handles).issubset(current_evidence)
                or metric.get("aggregation") not in _AGGREGATIONS
                or any(metric.get(target) != payload.get(source) for target, source in (
                    ("field", "measure_field"), ("aggregation", "aggregation"), ("unit", "unit"),
                    ("name", "name"), ("description", "description"),
                ))):
            continue
        metrics.append({**metric, "query_guidance": _metric_query_guidance(
            snapshot, connection=connection, connection_id=connection_id, asset=asset,
            field=fields[0], metric=metric, proposal=proposal,
        )})
    result = {
        "connection_id": connection_id, "source_kind": connection["kind"],
        "snapshot_id": snapshot.get("id"), "snapshot_version": snapshot.get("version"),
        "agent_ready": snapshot.get("agent_ready") is True,
        "datasets": [{"asset_id": item["id"], "name": item.get("name"),
                      "source_pointer": item.get("source_pointer"), "asset_type": item.get("asset_type"),
                      "description": item.get("description"), "description_status": item.get("description_status"),
                      "grain": item.get("grain_contract") or item.get("grain"),
                      "tenant_boundary": item.get("tenant_boundary"),
                      "row_count": (item.get("profile") or {}).get("row_count"),
                      "row_count_estimate": (item.get("profile") or {}).get("row_count_estimate"),
                      "row_count_estimate_state": (item.get("profile") or {}).get("row_count_estimate_state"),
                      "fields": item.get("schema") or [],
                      "query_examples": item.get("query_examples") or []} for item in assets],
        "joins": [item for item in snapshot.get("relationships", []) if isinstance(item, dict)
                  and item.get("predicate") == "joins_to" and item.get("status") == "approved"],
        "metrics": metrics,
        "tenant_boundaries": [item.get("tenant_boundary") for item in assets if isinstance(item, dict) and item.get("tenant_boundary")],
        "cross_source_joins": [item for item in snapshot.get("relationships", [])
                               if isinstance(item, dict) and item.get("predicate") == "cross_source_joins_to"
                               and item.get("status") == "approved"],
        "term_bindings": [item for item in snapshot.get("relationships", [])
                          if isinstance(item, dict) and item.get("assertion_kind") == "term_binding"
                          and item.get("status") == "approved"],
        "upstream_definitions": upstream_definitions,
        "glossary": [{"concept_id": item.get("id"), "term": item.get("name"),
                      "definition": item.get("definition"), "definition_status": item.get("definition_status"),
                      "evidence_ids": item.get("definition_evidence_ids") or item.get("evidence_ids") or []}
                     for item in snapshot.get("concepts", []) if isinstance(item, dict)
                     and item.get("semantic_type") == "document_business_term"],
        "cited_glossary": cited_glossary(store, tenant_id=tenant_id, connection_id=connection_id,
                                        expected_snapshot_id=snapshot.get("id")),
        "cited_upstream_definitions": cited_upstream_definitions(
            store, tenant_id=tenant_id, connection_id=connection_id, expected_snapshot_id=snapshot.get("id")),
        "cited_inventory_evidence": cited_inventory_evidence(
            store, tenant_id=tenant_id, connection_id=connection_id, expected_snapshot_id=snapshot.get("id")),
        "approved_assertions": [{**item, "trust_state": "owner_reviewed"} for item in assertions if item.get("status") == "approved"
                                and item.get("effective_on_current_snapshot") is not False],
        "stale_assertions": [{**item, "trust_state": "stale_review"} for item in assertions if item.get("status") == "stale_review"
                             or (item.get("status") == "approved"
                                 and item.get("effective_on_current_snapshot") is False)],
        "pending_assertions": [{**item, "trust_state": "pending_review"} for item in assertions
                               if item.get("status") == "pending_review"],
        "warning": "Approved assertions are owner-reviewed metadata. SQL examples remain unexecuted until independently verified.",
    }
    # Dependency state can change without creating a new target snapshot ID.
    # Refuse a packet assembled from an earlier effective approval projection.
    observed = store.get_connection(tenant_id, connection_id)
    if (not observed or any(observed.get(key) != connection.get(key)
                           for key in ("kind", "status", "config", "last_error"))
            or (observed.get("snapshot") or {}) != snapshot):
        from knowledge_layer.store import KnowledgeConflict

        raise KnowledgeConflict("Source contract changed during assembly; reload current meaning")
    return result


def search_source_evidence(
    store: Any, *, tenant_id: str, connection_id: str, query: str, limit: int = 5,
) -> dict[str, Any]:
    """Find bounded source metadata and document excerpts for cross-source curation."""
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 120 or not 1 <= limit <= 10:
        raise ValueError("query must be 2-120 characters and limit must be 1-10")
    connection = store.get_connection(tenant_id, connection_id)
    if not connection:
        raise ValueError("Connection was not found")
    snapshot = connection.get("snapshot") or {}
    words = set(normalize_name(query).split())
    matches = []
    for item in snapshot.get("evidence", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        summary = str(item.get("summary") or "")
        details = item.get("details") or {}
        terms = set(normalize_name(" ".join((summary, str(details.get("field") or ""),
                                              str(details.get("term") or "")))).split())
        score = len(words & terms)
        if score:
            matches.append((score, item))
    matches.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return {"connection_id": connection_id, "query": query, "matches": [
        {"id": item["id"], "asset_id": item.get("asset_id"),
         "evidence_type": item.get("evidence_type"),
         "source_pointer": item.get("source_pointer"), "summary": str(item.get("summary") or "")[:500],
         "trust_state": item.get("trust_state")}
        for _, item in matches[:limit]
    ], "total_matches": len(matches), "snapshot_id": snapshot.get("id")}


def search_document_evidence(
    store: Any, *, tenant_id: str, query: str, limit: int = 5,
) -> dict[str, Any]:
    """Search current connected documents and return portable evidence refs."""
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 120 or not 1 <= limit <= 10:
        raise ValueError("query must be 2-120 characters and limit must be 1-10")
    words = set(normalize_name(query).split())
    stop_words = {"a", "an", "the", "and", "or", "of", "for", "in", "to", "is", "are",
                  "what", "which", "how", "does"}
    words = words - stop_words or words

    matches: list[tuple[tuple[int, str, str, int], dict[str, Any]]] = []
    total_matches = 0
    scanned = 0
    incomplete = 0
    unknown_coverage = 0
    for source in store.iter_current_evidence_sources(tenant_id):
        for item in source["evidence"]:
            if not isinstance(item, dict) or item.get("evidence_type") != "document_derived_term":
                continue
            scanned += 1
            summary = str(item.get("summary") or "")
            details = item.get("details") or {}
            excerpt = str(details.get("source_excerpt") or "")
            content = normalize_name(" ".join((str(details.get("term") or ""), excerpt, summary)))
            content_words = set(content.split())
            if details.get("extraction_complete") is False or details.get("retention_complete") is False:
                incomplete += 1
            elif details.get("extraction_complete") is None or details.get("retention_complete") is None:
                unknown_coverage += 1
            # A single generic token such as "identity" cannot answer "player identity".
            if words and words.issubset(content_words):
                score = len(words) + int(normalize_name(query) in content)
                total_matches += 1
                match = {
                    "connection_id": source["connection_id"], "connection_name": source["connection_name"],
                    "asset_id": item.get("asset_id"), "source_pointer": item.get("source_pointer"),
                    "summary": summary[:500],
                    "excerpt": excerpt[:500] if excerpt else None,
                    "excerpt_truncated": len(excerpt) > 500,
                    "source_locator": details.get("source_locator"),
                    "source_revision": details.get("source_revision"),
                    "extraction_complete": details.get("extraction_complete"),
                    "retention_complete": details.get("retention_complete"),
                    "evidence_ref": {"connection_id": source["connection_id"],
                                     "snapshot_id": source["snapshot_id"],
                                     "evidence_id": item["id"]},
                }
                key = (-score, source["connection_id"], str(item["id"]), total_matches)
                insort(matches, (key, match))
                if len(matches) > limit:
                    matches.pop()
    return {"query": query, "matches": [match for _, match in matches],
            "total_matches": total_matches, "search_mode": "lexical_all_query_terms",
            "query_terms": sorted(words), "document_evidence_scanned": scanned,
            "incomplete_text_evidence": incomplete, "unknown_text_coverage_evidence": unknown_coverage,
            "search_scope": "Current authorized projected headings and explicitly retained source passages. "
            "A missing match is not proof that the full source document lacks the term."}


def apply_approved_assertion(
    snapshot: dict[str, Any], proposal: dict[str, Any], *, external_evidence_current: bool = True,
) -> None:
    """Project an owner-approved, still-current assertion into the shared snapshot.

    A changed source revision changes its evidence IDs. Such a proposal stays
    visible in history but does not silently govern the new source version.
    """
    assets = {item.get("id"): item for item in snapshot.get("assets", []) if isinstance(item, dict)}
    evidence = {item.get("id") for item in snapshot.get("evidence", []) if isinstance(item, dict)}
    asset = assets.get(proposal.get("asset_id"))
    handles = proposal.get("evidence_ids") or []
    if not asset or not handles or not set(handles).issubset(evidence) or not external_evidence_current:
        proposal["effective_on_current_snapshot"] = False
        return
    kind = proposal.get("assertion_kind")
    payload = proposal.get("payload") or {}
    proposal["effective_on_current_snapshot"] = True
    if kind == "dataset_description":
        asset.update({"description": payload["description"], "description_status": "approved",
                      "description_evidence_ids": handles, "description_proposal_id": proposal["id"]})
    elif kind == "field_description":
        field = next((item for item in asset.get("schema", []) if item.get("name") == payload.get("field")), None)
        if field is None:
            proposal["effective_on_current_snapshot"] = False
            return
        field.update({"description": payload["description"], "description_status": "approved",
                      "description_evidence_ids": handles, "description_proposal_id": proposal["id"]})
    elif kind == "grain":
        if asset.get("asset_type") != "table" or not set(payload.get("key_fields") or []).issubset(_fields(asset)):
            proposal["effective_on_current_snapshot"] = False
            return
        if "partition_keys" in payload and not set(payload.get("partition_keys") or []).issubset(_fields(asset)):
            proposal["effective_on_current_snapshot"] = False
            return
        asset["grain_contract"] = {**payload, "status": "approved", "evidence_ids": handles,
                                   "proposal_id": proposal["id"]}
    elif kind == "tenant_boundary":
        asset["tenant_boundary"] = {**payload, "status": "approved", "evidence_ids": handles,
                                    "proposal_id": proposal["id"]}
    elif kind == "join_rule":
        target = assets.get(payload.get("to_asset_id"))
        if (asset.get("asset_type") != "table" or not target or target.get("asset_type") != "table"
                or not set(payload.get("from_columns") or []).issubset(_fields(asset))
                or not set(payload.get("to_columns") or []).issubset(_fields(target))):
            proposal["effective_on_current_snapshot"] = False
            return
        relation_id = f"contract:{proposal['id']}"
        if not any(item.get("id") == relation_id for item in snapshot.get("relationships", [])):
            snapshot.setdefault("relationships", []).append({
                "id": relation_id, "subject_id": asset["id"], "object_id": payload["to_asset_id"],
                "predicate": "joins_to", "status": "approved", "trust_state": "owner_reviewed",
                "source_asset_id": asset["id"], "target_asset_id": payload["to_asset_id"],
                "source_field": ", ".join(payload["from_columns"]),
                "target_field": ", ".join(payload["to_columns"]),
                "cardinality": payload["cardinality"], "caveat": payload["caveat"],
                "evidence_ids": handles, "decision_proposal_id": proposal["id"], "certified": False,
            })
    elif kind == "metric":
        from knowledge_layer.metric_semantics import validate_metric_payload

        try:
            validated = validate_metric_payload(payload, asset.get("schema") or [])
        except KnowledgeValidationError:
            proposal["effective_on_current_snapshot"] = False
            return
        if asset.get("asset_type") != "table" or validated != payload:
            proposal["effective_on_current_snapshot"] = False
            return
        metric_id = f"contract:{proposal['id']}"
        if not any(item.get("id") == metric_id for item in snapshot.get("metrics", [])):
            snapshot.setdefault("metrics", []).append({
                "id": metric_id, "name": payload["name"], "description": payload["description"],
                "source_asset_id": asset["id"], "field": payload["measure_field"],
                "aggregation": payload["aggregation"], "unit": payload["unit"],
                "status": "approved", "trust_state": "owner_reviewed", "certified": False,
                "evidence_ids": handles, "decision_proposal_id": proposal["id"],
                "semantics": payload,
            })
    elif kind == "cross_source_join":
        from knowledge_layer.cross_source import source_binding_current

        if not source_binding_current(snapshot, payload):
            proposal["effective_on_current_snapshot"] = False
            return
        snapshot.setdefault("relationships", []).append({
            "id": f"contract:{proposal['id']}", "predicate": "cross_source_joins_to",
            "subject_id": asset["id"], "object_id": payload["to_asset_id"],
            **payload, "status": "approved", "trust_state": "owner_reviewed",
            "evidence_ids": handles, "evidence_refs": proposal.get("evidence_refs") or [],
            "decision_proposal_id": proposal["id"], "certified": False,
        })
    elif kind == "term_binding":
        try:
            validated = validate_assertion(snapshot, kind=kind, asset_id=asset["id"], payload=payload,
                                           evidence_ids=handles, source_kind="")
        except (KnowledgeValidationError, TypeError):
            proposal["effective_on_current_snapshot"] = False
            return
        if validated != payload:
            proposal["effective_on_current_snapshot"] = False
            return
        snapshot.setdefault("relationships", []).append({
            "id": f"contract:{proposal['id']}", "assertion_kind": "term_binding",
            "subject_id": asset["id"], "object_id": payload["target_concept_id"],
            "source_asset_id": asset["id"], "source_field": payload.get("field"),
            "target_concept_id": payload["target_concept_id"],
            "to_connection_id": payload["target_connection_id"], "to_snapshot_id": payload["target_snapshot_id"],
            "predicate": payload["relationship"], "status": "approved", "trust_state": "owner_reviewed",
            "evidence_ids": handles, "evidence_refs": proposal.get("evidence_refs") or [],
            "decision_proposal_id": proposal["id"], "certified": False, "executable": False,
        })
    elif kind == "query_example":
        examples = asset.setdefault("query_examples", [])
        if not any(item.get("proposal_id") == proposal["id"] for item in examples):
            examples.append({**payload, "proposal_id": proposal["id"],
                             "status": "approved_unverified", "evidence_ids": handles})


def strip_materialized_assertions(snapshot: dict[str, Any]) -> None:
    """Remove read-time contract overlays before recomputing current decisions."""
    for asset in snapshot.get("assets", []):
        if not isinstance(asset, dict):
            continue
        if asset.pop("description_proposal_id", None):
            for key in ("description", "description_status", "description_evidence_ids"):
                asset.pop(key, None)
        if isinstance(asset.get("grain_contract"), dict) and asset["grain_contract"].get("proposal_id"):
            asset.pop("grain_contract", None)
        if isinstance(asset.get("tenant_boundary"), dict) and asset["tenant_boundary"].get("proposal_id"):
            asset.pop("tenant_boundary", None)
        asset["query_examples"] = [item for item in asset.get("query_examples", [])
                                   if isinstance(item, dict) and not item.get("proposal_id")]
        for field in asset.get("schema", []):
            if isinstance(field, dict) and field.pop("description_proposal_id", None):
                for key in ("description", "description_status", "description_evidence_ids"):
                    field.pop(key, None)
    snapshot["relationships"] = [item for item in snapshot.get("relationships", [])
                                 if not (isinstance(item, dict) and str(item.get("id", "")).startswith("contract:"))]
    snapshot["metrics"] = [item for item in snapshot.get("metrics", [])
                           if not (isinstance(item, dict) and str(item.get("id", "")).startswith("contract:"))]


def ossie_preview(store: Any, *, tenant_id: str, connection_id: str) -> dict[str, Any]:
    """Validate a draft Apache Ossie core projection against a pinned schema.

    This is an interchange *preview*, never a certification or query runtime.
    Documents, evidence, and the ontology remain in the internal contract/RDF.
    """
    from jsonschema import Draft202012Validator

    contract = semantic_contract(store, tenant_id=tenant_id, connection_id=connection_id)
    if contract["source_kind"] not in {"postgres", "sql_catalog"}:
        return {"status": "not_queryable", "reason": "Ossie core datasets require SQL table bindings; use get_semantic_contract for file and document sources.",
                "connection_id": connection_id, "model": None}
    datasets = [item for item in contract["datasets"] if item["asset_type"] == "table"]
    if not datasets:
        return {"status": "no_datasets", "connection_id": connection_id, "model": None}
    names: dict[str, str] = {}
    for item in datasets:
        base = re.sub(r"[^a-z0-9_]+", "_", normalize_name(str(item["name"])).replace(" ", "_")) or "dataset"
        names[item["asset_id"]] = f"{base}_{item['asset_id'][-6:]}"
    model: dict[str, Any] = {
        "version": "0.2.0.dev0",
        "name": "knowledge_" + re.sub(r"[^a-z0-9_]+", "_", normalize_name(connection_id).replace(" ", "_")),
        "description": "Evidence-bound semantic contract preview; only owner-reviewed meaning is included.",
        "ai_context": {"instructions": "This is a draft interchange preview. Verify source access, scope, and query semantics before use."},
        "datasets": [], "relationships": [], "metrics": [],
    }
    for item in datasets:
        pointer = str(item.get("source_pointer") or "")
        parts = pointer.split("/")
        source = ".".join(parts[-2:]) if len(parts) >= 3 else pointer
        dataset: dict[str, Any] = {"name": names[item["asset_id"]], "source": source, "fields": []}
        if item.get("description_status") == "approved" and item.get("description"):
            dataset["description"] = item["description"]
        grain = item.get("grain") or {}
        if grain.get("status") == "approved" and grain.get("key_fields"):
            dataset["primary_key"] = grain["key_fields"]
        for field in item.get("fields") or []:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            field_name = str(field["name"])
            projected: dict[str, Any] = {
                "name": field_name,
                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": '"' + field_name.replace('"', '""') + '"'}]},
            }
            data_type = {"text": "String", "integer": "Integer", "number": "Decimal", "boolean": "Boolean",
                         "date": "Date", "timestamp": "DateTimeTz"}.get(field.get("data_type"))
            if data_type:
                projected["datatype"] = data_type
            if field.get("description_status") == "approved" and field.get("description"):
                projected["description"] = field["description"]
            dataset["fields"].append(projected)
        model["datasets"].append(dataset)
    for join in contract["joins"]:
        if join.get("subject_id") not in names or join.get("object_id") not in names:
            continue
        if join.get("cardinality") not in {"many_to_one", "one_to_one"}:
            continue
        model["relationships"].append({
            "name": f"join_{join['id'][-12:]}", "from": names[join["subject_id"]],
            "to": names[join["object_id"]],
            "from_columns": [part.strip() for part in join["source_field"].split(",")],
            "to_columns": [part.strip() for part in join["target_field"].split(",")],
            "ai_context": {"instructions": join.get("caveat") or "Check join scope before use."},
        })
    omitted = []
    for metric in contract["metrics"]:
        if (metric.get("semantics") or {}).get("schema_version") in {"metric-v2", "metric-composite-v1"}:
            omitted.append({"metric_id": metric["id"], "reason": "typed_metric_semantics_need_explicit_interchange_mapping"})
            continue
        if metric.get("source_asset_id") not in names or not metric.get("field"):
            continue
        aggregation = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX",
                       "count": "COUNT", "count_distinct": "COUNT"}.get(metric.get("aggregation"))
        if not aggregation:
            continue
        field_ref = names[metric["source_asset_id"]] + '."' + str(metric["field"]).replace('"', '""') + '"'
        expression = f"{aggregation}({'DISTINCT ' if metric['aggregation'] == 'count_distinct' else ''}{field_ref})"
        model["metrics"].append({
            "name": re.sub(r"[^a-z0-9_]+", "_", normalize_name(str(metric["name"])).replace(" ", "_")),
            "description": metric.get("description") or "Owner-reviewed metric draft",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]},
        })
    schema_path = files("knowledge_layer").joinpath("schemas/ossie-schema-v0.2.0.dev0.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(model), key=lambda item: str(item.path))
    return {"status": "draft_valid" if not errors else "draft_invalid", "connection_id": connection_id,
            "spec_version": "0.2.0.dev0", "schema_source_commit": "b6c702ed1c07e91382a69e870c875cbd19570828",
            "schema_errors": [item.message for item in errors[:10]],
            "meaning_certified": False, "omitted": omitted, "lossless": not omitted,
            "model": model}
