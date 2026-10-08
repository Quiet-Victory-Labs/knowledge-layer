"""Source-neutral, deterministic metadata curation for the knowledge POC."""

from __future__ import annotations

import re
from typing import Any

from knowledge_layer.connectors import (
    external_adapter,
    google_drive,
    local_files,
    postgres,
    sql_catalog,
)
from knowledge_layer.connectors.common import ConnectorError, normalize_name, stable_id

try:
    from knowledge_layer.connectors import iceberg
except ModuleNotFoundError as exc:
    if exc.name != "pyiceberg":
        raise
    iceberg = None

try:
    from knowledge_layer.connectors import object_store
except ModuleNotFoundError as exc:
    if exc.name not in {"boto3", "botocore"}:
        raise
    object_store = None

CONNECTORS = {
    "local_files": local_files.discover,
    "postgres": postgres.discover,
    "sql_catalog": sql_catalog.discover,
    "external_adapter": external_adapter.discover,
    "google_drive": google_drive.discover,
}
if iceberg is not None:
    CONNECTORS["iceberg"] = iceberg.discover
if object_store is not None:
    CONNECTORS["object_store"] = object_store.discover

IDENTIFIER_RULES = (
    (re.compile(r"(^|_)(id|key|code|gtin|upc|ean|sku)$"), "identifier"),
    (re.compile(r"(^|_)(email|e_mail)(_|$)"), "contact_email"),
    (re.compile(r"(^|_)(phone|telephone|mobile)(_|$)"), "contact_phone"),
    (re.compile(r"(^|_)(postal_code|zip_code|country_code|site_code)(_|$)"), "geographic_code"),
    (re.compile(r"(^|_)(status|state|stage|category|type)(_|$)"), "categorical_status"),
    (re.compile(r"(^|_)(created_at|updated_at|opened_at|closed_at|event_time|timestamp)(_|$)"), "event_time"),
)
MEASURE_TERMS = {
    "amount",
    "balance",
    "cost",
    "count",
    "duration",
    "fee",
    "hours",
    "length",
    "mag",
    "magnitude",
    "price",
    "points",
    "percent",
    "percentage",
    "latency",
    "bytes",
    "temperature",
    "volume",
    "quantity",
    "qty",
    "rate",
    "revenue",
    "score",
    "spend",
    "total",
    "weight",
}
FIELD_SYNONYMS = {
    "customer": "party",
    "client": "party",
    "account": "party",
    "acct": "party",
    "supplier": "supplier",
    "vendor": "supplier",
    "product": "product",
    "item": "product",
    "employee": "worker",
    "staff": "worker",
}
MAX_EVIDENCE = 10_000
MAX_CONCEPTS = 10_000
MAX_PROPOSALS = 2_000


def _failure_snapshot(kind: str, config: dict[str, Any], error: ConnectorError) -> dict[str, Any]:
    source_id = config.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        source_id = stable_id("source", kind, config.get("name", "source"))
    source = {
        "id": source_id,
        "name": _safe_name(config.get("name"), kind),
        "kind": kind,
        "location": None,
        "revision": None,
        "trust_state": "unverified",
    }
    result = {
        "snapshot_id": None,
        "kind": kind,
        "status": "error",
        "discovery_complete": False,
        "meaning_certified": False,
        "agent_ready": False,
        "curation_status": "error",
        "source": source,
        "capabilities": _empty_capabilities(),
        "assets": [],
        "concepts": [],
        "relationships": [],
        "metrics": [],
        "proposals": [],
        "evidence": [],
        "drift": {"previous_snapshot_id": None, "added_assets": [], "removed_assets": [], "changed_assets": []},
        "errors": [{"code": error.code, "message": error.public_message, "source_pointer": error.source_pointer}],
    }
    return result


def _empty_capabilities() -> dict[str, bool]:
    return {
        "metadata": False,
        "safe_profiling": False,
        "documents": False,
        "change_signals": False,
        "query_execution": False,
        "permission_mirroring": False,
    }


def _safe_name(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        # Names are metadata supplied by the connection owner, never secret config.
        return re.sub(r"[\x00-\x1f]", " ", value.strip())[:120]
    return f"{fallback} source"


def _profile_for_field(asset: dict[str, Any], field: str) -> dict[str, Any]:
    profile = asset.get("profile") or {}
    return {
        "null_count": profile.get("null_counts", {}).get(field),
        "missing_count": profile.get("missing_counts", {}).get(field),
        "empty_count": profile.get("empty_counts", {}).get(field),
        "distinct_count": profile.get("distinct_counts", {}).get(field),
        "profile_rows": profile.get("profile_rows", 0),
        "row_count": profile.get("row_count"),
        "row_count_is_complete": bool(profile.get("row_count_is_complete")),
        "distinct_count_is_lower_bound": profile.get("distinct_counts_are_lower_bounds", {}).get(field, False),
    }


def _field_key(name: str) -> str:
    return normalize_name(name).replace(" ", "_")


def _semantic_rule(field: dict[str, Any]) -> tuple[str, float, str] | None:
    name = _field_key(str(field.get("name", "")))
    data_type = str(field.get("data_type", "unknown"))
    if not name:
        return None
    for pattern, semantic_type in IDENTIFIER_RULES:
        if pattern.search(name):
            confidence = 0.92 if semantic_type == "identifier" else 0.82
            return semantic_type, confidence, f"Field name matches the explainable `{semantic_type}` naming rule."
    if data_type in {"date", "timestamp"} and name.endswith("_at"):
        return "event_time", 0.75, "The timestamp field name indicates when a source event occurred; the business time rule is not confirmed."
    if re.search(r"(^|_)(date|time|at)$", name):
        return "temporal_attribute", 0.82, "The field name suggests a time attribute; its business time rule is not confirmed."
    if data_type in {"integer", "number", "decimal", "float", "double"}:
        return "numeric_attribute", 0.58, "The source schema declares a numeric field; its business meaning is not confirmed."
    if data_type in {"date", "timestamp"}:
        return "temporal_attribute", 0.7, "The source schema declares a date or timestamp field."
    return None


def _unit_for_field(name: str) -> str | None:
    words = set(_field_key(name).split("_"))
    if words & {"mag", "magnitude"}:
        return "magnitude_scale_unspecified"
    if words & {"usd", "eur", "gbp", "currency"}:
        return next((code.upper() for code in ("usd", "eur", "gbp") if code in words), None)
    if words & {"cost", "price", "amount", "revenue", "spend", "fee", "balance"}:
        return "currency_unspecified"
    if words & {"hour", "hours", "hr", "hrs"}:
        return "hour"
    if words & {"day", "days"}:
        return "day"
    if words & {"kg", "kilogram", "kilograms"}:
        return "kg"
    if words & {"lb", "lbs", "pound", "pounds"}:
        return "lb"
    if words & {"count", "quantity", "qty"}:
        return "count"
    if words & {"pct", "percent", "percentage"}:
        return "percent"
    return None


def _metric_name(field_name: str) -> str:
    label = normalize_name(field_name)
    if not label:
        return "Unnamed numeric measure"
    return f"{label.title()}"


def _is_measure(field: dict[str, Any], asset: dict[str, Any]) -> bool:
    data_type = str(field.get("data_type", "unknown"))
    if data_type not in {"integer", "number", "decimal", "float", "double"}:
        return False
    words = set(_field_key(str(field.get("name", ""))).split("_"))
    semantic = _semantic_rule(field)
    if semantic and semantic[0] in {"identifier", "event_time", "temporal_attribute", "categorical_status"}:
        return False
    if words & MEASURE_TERMS:
        return True
    return words == {"value"} and any(
        _field_key(str(candidate.get("name", ""))) in {"indicator_id", "measure_id", "metric_id"}
        for candidate in asset.get("schema", [])
    )


def _entity_name(asset_name: str) -> str:
    stem = asset_name.rsplit(".", 1)[0]
    words = normalize_name(stem).split()
    if words and words[-1].endswith("ies"):
        words[-1] = f"{words[-1][:-3]}y"
    elif words and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(word.title() for word in words) or "Source Entity"


def _candidate_key_fields(asset: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    declared = asset.get("profile", {}).get("primary_key")
    declared = declared if isinstance(declared, list) else []
    for field in asset.get("schema", []):
        name = str(field.get("name", ""))
        normalized = _field_key(name)
        if name not in declared and normalized != "id" and not normalized.endswith("_id") and not normalized.endswith("_key"):
            continue
        field_profile = _profile_for_field(asset, name)
        sampled_all_rows = (
            field_profile.get("row_count_is_complete")
            and field_profile.get("profile_rows") == field_profile.get("row_count")
        )
        unique_observed = (
            sampled_all_rows
            and isinstance(field_profile.get("row_count"), int) and field_profile["row_count"] > 0
            and field_profile.get("distinct_count") == field_profile.get("row_count")
            and field_profile.get("null_count") == 0
            and field_profile.get("missing_count") in {None, 0}
            and field_profile.get("empty_count") in {None, 0}
            and not field_profile.get("distinct_count_is_lower_bound")
        )
        candidates.append(
            {
                "field": name,
                "status": "unique_in_profile" if unique_observed else (
                    "declared_primary_key" if len(declared) == 1 else "declared_primary_key_component"
                ) if name in declared else "candidate_by_name",
                "evidence_id": None,
                "profile": field_profile,
            }
        )
    return candidates


def _term_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:96]


def _append_upstream_definitions(
    assets: list[dict[str, Any]], evidence: list[dict[str, Any]], concepts: list[dict[str, Any]],
) -> None:
    """Project native definitions as separate reviewable, revision-bound concepts."""
    for asset in assets:
        upstream = asset.get("upstream_definition")
        if asset.get("asset_type") != "semantic_definition" or not isinstance(upstream, dict):
            continue
        evidence_id = stable_id("evidence", asset["id"], "upstream_definition", asset["revision"])
        # Observation clocks are retained on the asset/concept, but advancing a
        # clock alone must not change the meaning evidence a steward reviewed.
        binding = {key: value for key, value in upstream.items() if key != "source_observed_at"}
        evidence.append({
            "id": evidence_id, "asset_id": asset["id"], "source_pointer": asset["source_pointer"],
            "evidence_type": "upstream_semantic_definition",
            "summary": f"Upstream {upstream['kind']} {upstream['name']} in {upstream['namespace']} declares: {upstream['definition']}",
            "details": {"upstream_definition": binding, "source_revision": asset["revision"]},
            "trust_state": "observed_upstream_definition",
        })
        concepts.append({
            "id": stable_id("concept", asset["id"], "upstream_definition"),
            "name": upstream["name"], "semantic_type": "upstream_" + upstream["kind"],
            "status": "candidate", "trust_state": "observed_upstream_definition",
            "definition": None, "upstream_definition": upstream,
            "candidate_definitions": [{"text": upstream["definition"], "status": "upstream_declared", "evidence_id": evidence_id}],
            "source_binding": {"asset_id": asset["id"], "source_pointer": asset["source_pointer"], "status": "observed_metadata"},
            "evidence_ids": [evidence_id],
            "provenance": [{"asset_id": asset["id"], "source_pointer": asset["source_pointer"],
                            "upstream_id": upstream["id"], "namespace": upstream["namespace"], "version": upstream["version"]}],
        })


def _append_document_candidates(
    assets: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> None:
    terms: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        if asset.get("asset_type") != "document":
            continue
        profile = asset.get("profile", {})
        signals = profile.get("document_signals", {})
        claims_by_term: dict[str, list[dict[str, Any]]] = {}
        for claim in signals.get("quantified_claims", []):
            normalized = normalize_name(_term_text(claim.get("term")))
            if normalized:
                claims_by_term.setdefault(normalized, []).append(claim)
        descriptions_by_term: dict[str, list[dict[str, Any]]] = {}
        for description in signals.get("candidate_descriptions", []):
            if not isinstance(description, dict) or not isinstance(description.get("excerpt"), str):
                continue
            normalized = normalize_name(_term_text(description.get("term")))
            if not normalized or len(evidence) >= MAX_EVIDENCE:
                continue
            chunk_id = stable_id("evidence", asset["id"], asset.get("revision"),
                                 "document_passage", normalized, description)
            evidence.append({
                "id": chunk_id, "asset_id": asset["id"],
                "source_pointer": asset["source_pointer"],
                "evidence_type": "document_derived_term",
                "summary": f"Document passage under {description['term']}: {description['excerpt']}",
                "details": {"term": description["term"], "source_excerpt_retained": True,
                            "source_excerpt": description["excerpt"],
                            "source_line": description.get("line"),
                            "source_locator": description.get("source_locator"),
                            "source_revision": asset.get("revision"),
                            "extraction_complete": profile.get("document_extraction", {}).get("complete"),
                            "retention_complete": profile.get("document_retention", {}).get("complete")},
                "trust_state": "observed_source_text",
            })
            descriptions_by_term.setdefault(normalized, []).append(description | {"evidence_id": chunk_id})
        emitted_terms: set[str] = set()
        for term_value in signals.get("candidate_terms", []):
            if len(evidence) >= MAX_EVIDENCE or len(concepts) >= MAX_CONCEPTS:
                break
            term = _term_text(term_value)
            normalized = normalize_name(term)
            if not normalized or normalized in emitted_terms:
                continue
            emitted_terms.add(normalized)
            claims = claims_by_term.get(normalized, [])
            descriptions = descriptions_by_term.get(normalized, [])
            evidence_id = stable_id("evidence", asset["id"], asset.get("revision"),
                                    "document_term", normalized, claims)
            evidence.append(
                {
                    "id": evidence_id,
                    "asset_id": asset["id"],
                    "source_pointer": asset["source_pointer"],
                    "evidence_type": "document_derived_term",
                    "summary": f"Document heading or filename supports the candidate business term: {term}.",
                    "details": {
                        "term": term,
                        "quantified_claims": [
                            {"value": claim.get("value"), "unit": claim.get("unit"), "rule": claim.get("rule"),
                             "source_locator": claim.get("source_locator")}
                            for claim in claims
                        ],
                        "source_excerpt_retained": False,
                        "source_excerpt": None, "source_line": None,
                        "source_locator": signals.get("candidate_term_locators", {}).get(term),
                        "source_revision": asset.get("revision"),
                        "extraction_complete": profile.get("document_extraction", {}).get("complete"),
                        "retention_complete": profile.get("document_retention", {}).get("complete"),
                    },
                    "trust_state": "observed_source_text",
                }
            )
            concept_id = stable_id("concept", asset["id"], "document_term", normalized)
            concept = {
                "id": concept_id,
                "name": term,
                "semantic_type": "document_business_term",
                "status": "candidate",
                "trust_state": "inferred",
                "confidence": 0.72,
                "definition": None,
                "candidate_definitions": [
                    {"value": claim.get("value"), "unit": claim.get("unit")} for claim in claims
                ] + [{"text": description["excerpt"], "source_line": description.get("line"),
                       "source_locator": description.get("source_locator"),
                       "evidence_id": description["evidence_id"], "status": "unreviewed_source_text"}
                      for description in descriptions[:6]],
                "candidate_definition_excerpt_count": len(descriptions),
                "candidate_definition_excerpt_limit": 6,
                "evidence_ids": [evidence_id] + [description["evidence_id"] for description in descriptions],
                "provenance": [{"asset_id": asset["id"], "source_pointer": asset["source_pointer"]}],
            }
            concepts.append(concept)
            terms.setdefault(normalized, []).append(
                {
                    "concept": concept,
                    "asset": asset,
                    "claims": claims,
                    "evidence_id": evidence_id,
                    "term": term,
                }
            )

    for normalized, entries in sorted(terms.items()):
        if len(entries) < 2:
            continue
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if left["asset"]["id"] == right["asset"]["id"]:
                    continue
                left_values = {(claim.get("value"), claim.get("unit")) for claim in left["claims"]}
                right_values = {(claim.get("value"), claim.get("unit")) for claim in right["claims"]}
                has_conflict = bool(left_values and right_values and left_values != right_values)
                proposal_type = "definition_conflict" if has_conflict else "possible_duplicate_term"
                evidence_ids = [left["evidence_id"], right["evidence_id"]]
                relation_id = stable_id(
                    "relationship",
                    left["concept"]["id"],
                    "conflicts_with" if has_conflict else "may_be_duplicate_of",
                    right["concept"]["id"],
                )
                relationship = {
                    "id": relation_id,
                    "subject_id": left["concept"]["id"],
                    "predicate": "conflicts_with" if has_conflict else "may_be_duplicate_of",
                    "object_id": right["concept"]["id"],
                    "source_asset_id": left["asset"]["id"],
                    "target_asset_id": right["asset"]["id"],
                    "status": "candidate",
                    "ambiguity": "conflicting_definitions" if has_conflict else "possible_duplicate",
                    "confidence": 0.78 if has_conflict else 0.65,
                    "rationale": "Matching document headings identify the same term; extracted claims differ." if has_conflict else "Matching document headings may describe the same business term; source ownership and scope are unresolved.",
                    "evidence_ids": evidence_ids,
                }
                relationships.append(relationship)
                proposals.append(
                    {
                        "id": stable_id("proposal", relation_id),
                        "proposal_type": proposal_type,
                        "candidate_id": relation_id,
                        "subject_id": relationship["subject_id"],
                        "predicate": relationship["predicate"],
                        "object_id": relationship["object_id"],
                        "status": "pending_review",
                        "confidence": relationship["confidence"],
                        "ambiguity": relationship["ambiguity"],
                        "candidate_claims": [
                            {
                                "source_pointer": entry["asset"]["source_pointer"],
                                "values": [
                                    {"value": claim.get("value"), "unit": claim.get("unit")}
                                    for claim in entry["claims"]
                                ],
                            }
                            for entry in (left, right)
                        ],
                        "rationale": relationship["rationale"],
                        "evidence_ids": evidence_ids,
                    }
                )
                if len(proposals) >= MAX_PROPOSALS:
                    return


def _append_source_assertions(
    source_id: str,
    assets: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> None:
    """Project explicitly sampled entity/edge rows as unverified source assertions."""
    entity_rows: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for asset in assets:
        samples = asset.get("profile", {}).get("semantic_samples", {})
        for row in samples.get("entities", []):
            entity_rows.setdefault(row["entity_id"], []).append((asset, row["label"]))
    entities: dict[str, tuple[dict[str, Any], str, str]] = {}
    for entity_id, rows in sorted(entity_rows.items()):
        if len(evidence) >= MAX_EVIDENCE or len(concepts) >= MAX_CONCEPTS:
            break
        labels = {label for _, label in rows}
        if len(labels) != 1:
            continue  # Conflicting labels cannot be collapsed into one identity.
        asset, label = rows[0]
        evidence_id = stable_id("evidence", asset["id"], "source_entity", entity_id, asset["revision"])
        concept_id = stable_id("concept", source_id, "source_entity", entity_id)
        evidence.append({
            "id": evidence_id, "asset_id": asset["id"], "source_pointer": asset["source_pointer"],
            "evidence_type": "sampled_source_entity", "summary": f"Source row labels entity {entity_id} as {label}.",
            "details": {"entity_id": entity_id, "label": label, "sampled": True},
            "trust_state": "observed_source_text",
        })
        concepts.append({
            "id": concept_id, "name": label, "semantic_type": "source_entity",
            "status": "candidate", "trust_state": "observed_source_text", "confidence": 0.9,
            "definition": None, "source_identifier": entity_id, "evidence_ids": [evidence_id],
            "provenance": [{"asset_id": asset["id"], "source_pointer": asset["source_pointer"]}],
        })
        entities[entity_id] = (asset, concept_id, evidence_id)
    for asset in assets:
        samples = asset.get("profile", {}).get("semantic_samples", {})
        for row in samples.get("edges", []):
            if len(relationships) >= MAX_PROPOSALS or len(evidence) >= MAX_EVIDENCE:
                return
            subject = entities.get(row["subject_id"])
            parent = entities.get(row["parent_id"])
            predicate = row["predicate"]
            if not subject or not parent or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", predicate):
                continue
            evidence_id = stable_id("evidence", asset["id"], "source_edge", row, asset["revision"])
            evidence.append({
                "id": evidence_id, "asset_id": asset["id"], "source_pointer": asset["source_pointer"],
                "evidence_type": "sampled_source_assertion",
                "summary": f"Source row asserts {row['subject_id']} {predicate} {row['parent_id']}.",
                "details": {**row, "sampled": True}, "trust_state": "observed_source_text",
            })
            relationships.append({
                "id": stable_id("relationship", source_id, "source_assertion", row),
                "subject_id": subject[1], "predicate": predicate, "object_id": parent[1],
                "source_asset_id": asset["id"], "target_asset_id": parent[0]["id"],
                "status": "observed_source_assertion", "trust_state": "observed_source_text",
                "confidence": 1.0, "certified": False,
                "rationale": "The sampled source row explicitly declares this edge; identity and business meaning are unreviewed.",
                "evidence_ids": [evidence_id, subject[2], parent[2]],
            })


def _synonym_proposals(
    assets: list[dict[str, Any]],
    field_concepts: dict[tuple[str, str], dict[str, Any]],
    field_evidence: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    by_synonym: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for asset in assets:
        for field in asset.get("schema", []):
            field_name = str(field.get("name", ""))
            normalized = _field_key(field_name)
            if not normalized.endswith("_id"):
                continue
            stem = normalized.removesuffix("_id").split("_")[-1]
            synonym_group = FIELD_SYNONYMS.get(stem)
            if synonym_group and (asset["id"], field_name) in field_concepts:
                by_synonym.setdefault(synonym_group, []).append((asset, field_name))
    proposals = []
    for group, entries in sorted(by_synonym.items()):
        for index, (left_asset, left_field) in enumerate(entries):
            for right_asset, right_field in entries[index + 1 :]:
                if len(proposals) >= MAX_PROPOSALS:
                    return proposals
                if left_asset["id"] == right_asset["id"] or left_field == right_field:
                    continue
                left = field_concepts[(left_asset["id"], left_field)]
                right = field_concepts[(right_asset["id"], right_field)]
                evidence_ids = [field_evidence[(left_asset["id"], left_field)], field_evidence[(right_asset["id"], right_field)]]
                proposal_id = stable_id("proposal", "possible_synonym", left["id"], right["id"])
                proposals.append(
                    {
                        "id": proposal_id,
                        "proposal_type": "possible_synonym",
                        "candidate_id": proposal_id,
                        "subject_id": left["id"],
                        "predicate": "may_be_synonym_of",
                        "object_id": right["id"],
                        "status": "pending_review",
                        "ambiguity": "unresolved",
                        "confidence": 0.62,
                        "rationale": f"The field names fall in the explainable `{group}` synonym group; equivalence still needs definition and source-owner review.",
                        "evidence_ids": evidence_ids,
                    }
                )
    return proposals


def _field_evidence(
    asset: dict[str, Any], field: dict[str, Any], field_profile: dict[str, Any], semantic_type: str
) -> dict[str, Any]:
    evidence_id = stable_id("evidence", asset["id"], field.get("name"), semantic_type, asset.get("revision"))
    return {
        "id": evidence_id,
        "asset_id": asset["id"],
        "source_pointer": asset["source_pointer"],
        "evidence_type": "schema_and_profile",
        "summary": f"Field {field.get('name')} has declared type {field.get('data_type')}; source metadata supports a {semantic_type} candidate whose meaning remains unverified.",
        "details": {
            "field": field.get("name"),
            "data_type": field.get("data_type"),
            "nullable": field.get("nullable"),
            **{key: field[key] for key in ("native_type", "field_id") if key in field},
            **field_profile,
            "source_values_retained": bool(asset.get("profile", {}).get("content_retained")),
        },
        "trust_state": "observed_metadata",
    }


def _drift(assets: list[dict[str, Any]], previous: Any) -> dict[str, Any]:
    if not isinstance(previous, dict):
        return {"previous_snapshot_id": None, "added_assets": [], "removed_assets": [], "changed_assets": []}
    previous_assets = {
        str(asset.get("id")): asset
        for asset in previous.get("assets", [])
        if isinstance(asset, dict) and asset.get("id")
    }
    current_assets = {str(asset["id"]): asset for asset in assets}
    added = sorted(set(current_assets) - set(previous_assets))
    removed = sorted(set(previous_assets) - set(current_assets))
    changed = []
    for asset_id in sorted(set(current_assets) & set(previous_assets)):
        old = previous_assets[asset_id]
        current = current_assets[asset_id]
        changes = []
        if old.get("revision") != current.get("revision"):
            changes.append("revision_changed")
        if old.get("schema") != current.get("schema"):
            changes.append("schema_changed")
        if old.get("profile") != current.get("profile"):
            changes.append("profile_changed")
        if changes:
            changed.append(
                {
                    "asset_id": asset_id,
                    "before_revision": old.get("revision"),
                    "after_revision": current.get("revision"),
                    "change_types": changes,
                }
            )
    return {
        "previous_snapshot_id": previous.get("snapshot_id"),
        "added_assets": added,
        "removed_assets": removed,
        "changed_assets": changed,
    }


def _curate(discovery: dict[str, Any], previous: Any) -> dict[str, Any]:
    assets = discovery["assets"]
    required_evidence = sum(1 + len(asset.get("schema", [])) + (asset.get("asset_type") == "semantic_definition") for asset in assets)
    if required_evidence > MAX_EVIDENCE:
        raise ConnectorError("curation_budget_exceeded", "Source metadata exceeds the curation evidence budget; narrow the selected scope")
    join_probes = {
        (item["source_asset_id"], item["target_asset_id"], item["source_field"], item["target_field"]): item
        for item in discovery.get("join_probes", []) if isinstance(item, dict)
    }
    evidence = []
    probe_evidence_ids: dict[tuple[str, str, str, str], str] = {}
    source_probe_ids: dict[str, list[str]] = {}
    assets_by_id = {asset["id"]: asset for asset in assets}
    # A measured overlap (including zero) is source evidence regardless of
    # whether name/grain heuristics suggest a relationship. Reserve its bounded
    # evidence first; it does not establish join direction or business identity.
    for probe_key, join_probe in sorted(join_probes.items()):
        if len(evidence) >= MAX_EVIDENCE:
            break
        source_asset = assets_by_id.get(join_probe["source_asset_id"])
        target_asset = assets_by_id.get(join_probe["target_asset_id"])
        if source_asset is None or target_asset is None:
            continue
        probe_id = stable_id(
            "evidence", "join_probe", join_probe,
            source_asset.get("revision"), target_asset.get("revision"),
        )
        evidence.append({
            "id": probe_id, "asset_id": source_asset["id"],
            "source_pointer": source_asset["source_pointer"],
            "evidence_type": "bounded_join_probe",
            "summary": (
                f"Bounded key overlap on {join_probe['source_field']}: "
                f"{join_probe['matched_source_rows']} of "
                f"{join_probe['source_nonnull_rows_probed']} non-null source rows match "
                f"{join_probe['target_field']} in {target_asset['name']}; "
                "this observation does not verify a join; no raw keys retained."
            ),
            "details": join_probe, "trust_state": "observed_profile",
        })
        probe_evidence_ids[probe_key] = probe_id
        source_probe_ids.setdefault(source_asset["id"], []).append(probe_id)
    concepts = []
    metrics = []
    proposals = []
    field_concepts: dict[tuple[str, str], dict[str, Any]] = {}
    field_evidence: dict[tuple[str, str], str] = {}
    entity_concepts: dict[str, dict[str, Any]] = {}

    for asset in assets:
        if len(evidence) >= MAX_EVIDENCE:
            break
        asset_evidence_id = stable_id("evidence", asset["id"], "asset_profile", asset.get("revision"))
        evidence.append(
            {
                "id": asset_evidence_id,
                "asset_id": asset["id"],
                "source_pointer": asset["source_pointer"],
                "evidence_type": "asset_metadata",
                "summary": f"Discovered {asset.get('asset_type')} metadata and schema for {asset.get('name')}.",
                "details": {
                    "schema_field_count": len(asset.get("schema", [])),
                    "profile": asset.get("profile", {}),
                    "revision": asset.get("revision"),
                    "observations": asset.get("observations", {}),
                    "source_values_retained": bool(asset.get("profile", {}).get("content_retained")),
                },
                "trust_state": "observed_metadata",
            }
        )
        if asset.get("asset_type") == "table":
            candidate_keys = _candidate_key_fields(asset)
            for key in candidate_keys:
                key["evidence_id"] = asset_evidence_id
            observed_keys = [key for key in candidate_keys if key["status"] == "unique_in_profile"]
            entity_id = stable_id("concept", asset["id"], "business_entity")
            entity = {
                "id": entity_id,
                "name": _entity_name(str(asset.get("name", ""))),
                "semantic_type": "business_entity",
                "status": "candidate",
                "trust_state": "inferred",
                "confidence": 0.78 if observed_keys else 0.62,
                "definition": None,
                "source_binding": {
                    "asset_id": asset["id"],
                    "source_pointer": asset["source_pointer"],
                    "status": "candidate",
                },
                "grain": {
                    "basis": "one_source_record",
                    "candidate_key_fields": candidate_keys,
                    "status": "candidate" if candidate_keys else "unknown",
                    "observed_unique_key_fields": [key["field"] for key in observed_keys],
                    "evidence_ids": [asset_evidence_id],
                },
                "evidence_ids": [asset_evidence_id, *source_probe_ids.get(asset["id"], [])[:2]],
                "provenance": [{"asset_id": asset["id"], "source_pointer": asset["source_pointer"]}],
            }
            concepts.append(entity)
            entity_concepts[asset["id"]] = entity
        for field in asset.get("schema", []):
            semantic = _semantic_rule(field)
            metric_candidate = _is_measure(field, asset)
            if len(evidence) >= MAX_EVIDENCE:
                break
            if metric_candidate:
                semantic_type, confidence, rationale = (
                    "numeric_measure_candidate",
                    0.62,
                    "The field name and numeric schema type match a candidate-measure rule; an indicator-dependent value may mix units and definitions.",
                )
            else:
                semantic_type, confidence, rationale = semantic or (
                    "source_column",
                    0.0,
                    "The source schema declares a field; its business meaning is not confirmed.",
                )
            field_name = str(field.get("name", ""))
            field_profile = _profile_for_field(asset, field_name)
            evidence_row = _field_evidence(asset, field, field_profile, semantic_type)
            evidence.append(evidence_row)
            field_evidence[(asset["id"], field_name)] = evidence_row["id"]
            if not semantic and not metric_candidate:
                continue
            concept_id = stable_id("concept", asset["id"], field_name, semantic_type)
            concept = {
                "id": concept_id,
                "name": _metric_name(field_name),
                "semantic_type": semantic_type,
                "status": "candidate",
                "trust_state": "inferred",
                "confidence": confidence,
                "definition": None,
                "evidence_ids": [evidence_row["id"]],
                "provenance": [{"asset_id": asset["id"], "source_pointer": asset["source_pointer"], "field": field_name}],
            }
            concepts.append(concept)
            field_concepts[(asset["id"], field_name)] = concept
            if metric_candidate and len(metrics) < MAX_PROPOSALS:
                metric_id = stable_id("metric", asset["id"], field_name)
                key_fields = [
                    candidate["name"]
                    for candidate in asset.get("schema", [])
                    if _field_key(str(candidate.get("name", ""))) == "id"
                    or _field_key(str(candidate.get("name", ""))).endswith("_id")
                    or candidate.get("name") in (asset.get("profile", {}).get("primary_key") or [])
                ]
                key_observed = bool(key_fields) and all(
                    _profile_for_field(asset, key).get("row_count_is_complete")
                    and isinstance(_profile_for_field(asset, key).get("row_count"), int)
                    and _profile_for_field(asset, key)["row_count"] > 0
                    and _profile_for_field(asset, key).get("profile_rows")
                    == _profile_for_field(asset, key).get("row_count")
                    and _profile_for_field(asset, key).get("distinct_count")
                    == asset.get("profile", {}).get("row_count")
                    and _profile_for_field(asset, key).get("null_count") == 0
                    and _profile_for_field(asset, key).get("missing_count") in {None, 0}
                    and _profile_for_field(asset, key).get("empty_count") in {None, 0}
                    for key in key_fields
                )
                grain = {
                    "basis": "one_source_record" if key_observed else "unknown",
                    "key_candidates": key_fields,
                    "status": "candidate" if key_fields else "unknown",
                }
                metric = {
                    "id": metric_id,
                    "name": _metric_name(field_name),
                    "source_asset_id": asset["id"],
                    "source_pointer": asset["source_pointer"],
                    "field": field_name,
                    "grain": grain,
                    "unit": _unit_for_field(field_name),
                    "status": "candidate",
                    "certified": False,
                    "confidence": 0.58,
                    "evidence_ids": [evidence_row["id"]],
                    "rationale": "A name-and-type rule identifies a possible measure; definition, filters, time rules, and grain need review.",
                }
                metrics.append(metric)
                proposals.append(
                    {
                        "id": stable_id("proposal", metric_id),
                        "proposal_type": "metric_definition",
                        "candidate_id": metric_id,
                        "subject_id": metric_id,
                        "predicate": "defines_candidate_metric",
                        "object_id": asset["id"],
                        "status": "pending_review",
                        "confidence": metric["confidence"],
                        "rationale": metric["rationale"],
                        "evidence_ids": metric["evidence_ids"],
                        "certified": False,
                    }
                )

    relationships = []
    id_fields: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for asset in assets:
        for field in asset.get("schema", []):
            name = str(field.get("name", ""))
            normalized = _field_key(name)
            if normalized.endswith("_id") and (asset["id"], name) in field_concepts:
                id_fields.setdefault(normalized, []).append((asset, field))
    for normalized_name, entries in sorted(id_fields.items()):
        for source_asset, source_field in entries:
            source_key = next(
                (item for item in _candidate_key_fields(source_asset) if item["field"] == source_field["name"]),
                {"status": "candidate_by_name"},
            )
            field_entity_name = normalize_name(normalized_name.removesuffix("_id"))
            source_name_matches = normalize_name(_entity_name(source_asset["name"])) == field_entity_name
            if source_key["status"] == "unique_in_profile" and source_name_matches:
                continue
            targets = [
                (target_asset, target_field)
                for target_asset, target_field in entries
                if target_asset["id"] != source_asset["id"]
                and any(
                    item["field"] == target_field["name"] and item["status"] == "unique_in_profile"
                    for item in _candidate_key_fields(target_asset)
                )
                and (
                    source_key["status"] != "unique_in_profile"
                    or normalize_name(_entity_name(target_asset["name"])) == field_entity_name
                )
            ]
            if not targets:
                continue
            ambiguity = len(targets) > 1
            source_field_concept = field_concepts[(source_asset["id"], str(source_field["name"]))]
            source_entity = entity_concepts[source_asset["id"]]
            for target_asset, target_field in targets:
                if len(relationships) >= MAX_PROPOSALS:
                    break
                target_field_concept = field_concepts[(target_asset["id"], str(target_field["name"]))]
                target_entity = entity_concepts[target_asset["id"]]
                evidence_ids = [
                    field_evidence[(source_asset["id"], str(source_field["name"]))],
                    field_evidence[(target_asset["id"], str(target_field["name"]))],
                ]
                probe_key = (source_asset["id"], target_asset["id"],
                             source_field["name"], target_field["name"])
                join_probe = join_probes.get(probe_key)
                if probe_id := probe_evidence_ids.get(probe_key):
                    evidence_ids.append(probe_id)
                confidence = 0.72 if ambiguity else 0.84
                target_key = next(
                    item for item in _candidate_key_fields(target_asset) if item["field"] == target_field["name"]
                )
                relationship_id = stable_id("relationship", source_entity["id"], "may_reference_entity", target_entity["id"], normalized_name)
                rationale = (
                    f"Both assets expose {normalized_name}; the target field is unique in its profile and the target entity name matches the field stem. This suggests a direction, but matching values and business meaning have not been verified."
                    if source_key["status"] == "unique_in_profile"
                    else f"Both assets expose {normalized_name}; the field is unique in the target profile and is not proven unique in the source profile. This suggests a direction, but matching values and business meaning have not been verified."
                )
                if join_probe:
                    rationale += " Bounded source-side key overlap was counted; historical identity and business scope remain unverified."
                relationship = {
                    "id": relationship_id,
                    "subject_id": source_entity["id"],
                    "predicate": "may_reference_entity",
                    "object_id": target_entity["id"],
                    "source_asset_id": source_asset["id"],
                    "target_asset_id": target_asset["id"],
                    "source_field": source_field["name"],
                    "target_field": target_field["name"],
                    "source_field_concept_id": source_field_concept["id"],
                    "target_field_concept_id": target_field_concept["id"],
                    "target_key_status": target_key["status"],
                    "join_probe": join_probe,
                    "status": "candidate",
                    "ambiguity": "multiple_targets" if ambiguity else None,
                    "confidence": confidence,
                    "rationale": rationale,
                    "evidence_ids": evidence_ids,
                }
                relationships.append(relationship)
                proposals.append(
                    {
                        "id": stable_id("proposal", relationship_id),
                        "proposal_type": "relationship_mapping",
                        "candidate_id": relationship_id,
                        "subject_id": relationship["subject_id"],
                        "predicate": relationship["predicate"],
                        "object_id": relationship["object_id"],
                        "status": "pending_review",
                        "confidence": confidence,
                        "ambiguity": relationship["ambiguity"],
                        "rationale": rationale,
                        "evidence_ids": evidence_ids,
                    }
                )

    # Source-declared foreign keys are observed technical relationships. They
    # still do not certify a business ontology mapping or metric grain.
    assets_by_pointer = {asset["source_pointer"]: asset for asset in assets}
    for asset in assets:
        source_entity = entity_concepts.get(asset["id"])
        if source_entity is None:
            continue
        for foreign_key in asset.get("profile", {}).get("foreign_keys", []):
            if len(relationships) >= MAX_PROPOSALS:
                break
            pointer_parts = asset["source_pointer"].split("/")
            if discovery["source"]["kind"] == "external_adapter" and foreign_key.get("target_pointer"):
                target_pointer = f"{discovery['source']['location']}/{foreign_key['target_pointer']}"
            elif len(pointer_parts) == 4 and pointer_parts[0] == "sql":
                target_pointer = "/".join((
                    pointer_parts[0], pointer_parts[1],
                    str(foreign_key.get("referred_schema") or pointer_parts[2]),
                    str(foreign_key.get("referred_table", "")),
                ))
            else:
                continue
            target_asset = assets_by_pointer.get(target_pointer)
            target_entity = entity_concepts.get(target_asset["id"]) if target_asset else None
            if target_entity is None:
                continue
            relation_id = stable_id("relationship", source_entity["id"], "declared_foreign_key", target_entity["id"],
                                   foreign_key.get("columns"), foreign_key.get("referred_columns", foreign_key.get("target_columns", [])))
            relationships.append({
                "id": relation_id,
                "subject_id": source_entity["id"],
                "predicate": "declared_foreign_key",
                "object_id": target_entity["id"],
                "source_asset_id": asset["id"],
                "target_asset_id": target_asset["id"],
                "source_field": ", ".join(foreign_key.get("columns", [])),
                "target_field": ", ".join(foreign_key.get("referred_columns", foreign_key.get("target_columns", []))),
                "status": "observed_metadata",
                "trust_state": "observed_metadata",
                "confidence": 1.0,
                "evidence_ids": [
                    stable_id("evidence", asset["id"], "asset_profile", asset.get("revision")),
                    stable_id("evidence", target_asset["id"], "asset_profile", target_asset.get("revision")),
                ],
                "rationale": "The source database declares this foreign-key constraint; its business meaning is not certified.",
            })

    synonym_proposals = _synonym_proposals(assets, field_concepts, field_evidence)
    proposals.extend(synonym_proposals)
    _append_upstream_definitions(assets, evidence, concepts)
    _append_document_candidates(assets, evidence, concepts, relationships, proposals)
    _append_source_assertions(discovery["source"]["id"], assets, evidence, concepts, relationships)

    duplicate_assets: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        if asset.get("asset_type") == "semantic_definition":
            continue
        duplicate_key = normalize_name(str(asset.get("name", "")))
        if duplicate_key:
            duplicate_assets.setdefault(duplicate_key, []).append(asset)
    for duplicate_key, entries in sorted(duplicate_assets.items()):
        for index, left in enumerate(entries):
            for right in entries[index + 1 :]:
                if len(proposals) >= MAX_PROPOSALS:
                    break
                if left["id"] == right["id"]:
                    continue
                evidence_ids = [
                    stable_id("evidence", left["id"], "asset_profile", left.get("revision")),
                    stable_id("evidence", right["id"], "asset_profile", right.get("revision")),
                ]
                proposal_id = stable_id("proposal", "possible_duplicate_asset", left["id"], right["id"])
                proposals.append(
                    {
                        "id": proposal_id,
                        "proposal_type": "possible_duplicate_asset",
                        "candidate_id": proposal_id,
                        "subject_id": left["id"],
                        "predicate": "may_be_duplicate_of",
                        "object_id": right["id"],
                        "status": "pending_review",
                        "confidence": 0.65,
                        "ambiguity": "unresolved",
                        "rationale": "Assets share a normalized name; scope, ownership, and row meaning still need review.",
                        "evidence_ids": evidence_ids,
                    }
                )

    drift = _drift(assets, previous)
    if any(len(values) >= limit for values, limit in (
        (evidence, MAX_EVIDENCE), (concepts, MAX_CONCEPTS),
        (relationships, MAX_PROPOSALS), (proposals, MAX_PROPOSALS), (metrics, MAX_PROPOSALS),
    )):
        raise ConnectorError("curation_budget_exceeded", "Source candidates reach the curation budget; narrow the selected scope")
    curation_errors = []
    for asset in assets:
        extraction = asset.get("profile", {}).get("document_extraction") or {}
        retention = asset.get("profile", {}).get("document_retention") or {}
        candidates = asset.get("profile", {}).get("document_candidates") or {}
        truncated = any(info.get("truncated") is True for info in (extraction, retention, candidates))
        unavailable = extraction.get("complete") is False or (
            retention.get("enabled") is True and retention.get("complete") is False) or candidates.get("complete") is False
        if truncated or unavailable:
            curation_errors.append({
                "code": "document_evidence_truncated" if truncated else "document_evidence_unavailable",
                "message": "Document evidence is incomplete within its extraction or retention boundary",
                "source_pointer": asset["source_pointer"],
            })
    normalized = {
        "kind": discovery["source"]["kind"],
        "source": discovery["source"],
        "capabilities": discovery["capabilities"],
        "assets": assets,
        "concepts": concepts,
        "relationships": relationships,
        "metrics": metrics,
        "proposals": proposals,
        "evidence": evidence,
        "drift": drift,
        "errors": discovery.get("errors", []),
    }
    # Drift points to the prior version and is refresh bookkeeping, not source
    # meaning. Excluding it gives unchanged source content a stable identity.
    snapshot_id = stable_id("snapshot", {key: value for key, value in normalized.items() if key != "drift"})
    has_assets = bool(assets)
    errors = discovery.get("errors", [])
    discovery_complete = not errors
    status = "discovery_complete" if discovery_complete else "partial_discovery" if has_assets else "error"
    if curation_errors and discovery_complete:
        status = "partial"
    return {
        "snapshot_id": snapshot_id,
        "status": status,
        "discovery_complete": discovery_complete,
        "curation_complete": not curation_errors and discovery_complete,
        "curation_errors": curation_errors,
        "meaning_certified": False,
        "agent_ready": False,
        "curation_status": "partial_curation" if curation_errors else "candidate_review_required" if discovery_complete else "partial_discovery",
        "profiling": discovery.get("profiling", {}),
        **normalized,
    }


def curate_source(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """Discover a bounded source and return a JSON-serializable candidate snapshot.

    Available kinds are registered in ``CONNECTORS``; optional dependencies may
    hide a connector from small installations.
    All inferred concepts, links, and metrics remain candidates for review.
    """
    if not isinstance(config, dict):
        return _failure_snapshot(str(kind), {}, ConnectorError("invalid_config", "config must be an object"))
    if not isinstance(kind, str) or kind not in CONNECTORS:
        return _failure_snapshot(str(kind), config, ConnectorError("unsupported_connector", "The requested source connector is not supported"))
    internal_config = dict(config)
    previous = internal_config.pop("previous_snapshot", internal_config.pop("_previous_snapshot", None))
    if kind == "local_files":
        internal_config["_previous_snapshot"] = previous
    try:
        discovery = CONNECTORS[kind](internal_config)
        return _curate(discovery, previous)
    except ConnectorError as exc:
        return _failure_snapshot(kind, internal_config, exc)
    except Exception:  # noqa: BLE001
        # Do not put driver exceptions in the snapshot: they can contain secrets,
        # absolute paths, or source-provided values.
        return _failure_snapshot(kind, internal_config, ConnectorError("connector_failed", "Source discovery failed; inspect server logs for the safe diagnostic"))
