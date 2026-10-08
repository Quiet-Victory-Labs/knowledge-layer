"""Exercise Knowledge Layer on audited, read-only Example metadata.

This does not query source databases or grade business answers. Adapter receipts
are discovery observations, not a steward-labeled semantic ground truth set.
Only connection/refresh state is written to the isolated knowledge registry.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from knowledge_layer.connectors.common import normalize_name, stable_id, type_name
from knowledge_layer.operator_secrets import read_owner_only_file

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("accounts",)


def _check(name: str, passed: bool, observed: Any = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "observed": observed}


def grade_refresh(connection: dict[str, Any], receipt: dict[str, Any], started_at: str) -> list[dict[str, Any]]:
    """An older healthy snapshot cannot qualify a failed current refresh."""
    current = connection.get("status") == "discovery_complete" and not connection.get("last_error")
    try:
        started = datetime.fromisoformat(started_at)
        observed = datetime.fromisoformat(receipt["observed_at"])
        refreshed = datetime.fromisoformat(connection["last_refreshed_at"])
        fresh = observed >= started and refreshed >= observed
    except (ValueError, TypeError, KeyError):
        fresh = False
    return [_check("current_refresh_succeeded", current),
            _check("receipt_and_snapshot_refreshed_this_run", fresh)]


def grade_connection_refresh(connection: dict[str, Any], started_at: str) -> list[dict[str, Any]]:
    try:
        fresh = datetime.fromisoformat(connection["last_refreshed_at"]) >= datetime.fromisoformat(started_at)
    except (ValueError, TypeError, KeyError):
        fresh = False
    return [_check("current_refresh_succeeded", connection.get("status") == "discovery_complete"
                   and not connection.get("last_error")),
            _check("snapshot_refreshed_this_run", fresh)]


def grade_native_iceberg(snapshot: dict[str, Any], receipt: dict[str, Any], *,
                         catalog: str, namespace: str, tables: list[str]) -> list[dict[str, Any]]:
    """Compare REST discovery with the audited Example catalog observation."""
    expected = {item["pointer"]: item for item in receipt.get("assets", [])}
    observed = {item["source_pointer"]: item for item in snapshot.get("assets", [])}
    selected = {f"{namespace}.{table}" for table in tables}
    checks = [_check("audited_scope_includes_native_selection", selected <= expected.keys()
                     and len(expected) == len(receipt.get("assets", []))),
              _check("native_exact_selected_assets", set(observed) == {f"{catalog}.{name}" for name in selected}
                     and len(observed) == len(snapshot.get("assets", []))),
              _check("native_discovery_and_curation_complete", snapshot.get("discovery_complete") is True
                     and snapshot.get("curation_complete") is True),
              _check("native_meaning_unreviewed", snapshot.get("meaning_certified") is False
                     and snapshot.get("agent_ready") is False)]
    mismatches = []
    for name in sorted(selected):
        wanted = expected.get(name, {})
        got = observed.get(f"{catalog}.{name}", {})
        fields = [{"name": col.get("name"), "data_type": type_name(col.get("data_type")),
                   "native_type": col.get("data_type"), "nullable": col.get("nullable"),
                   "field_id": col.get("field_id")} for col in wanted.get("schema", [])]
        facts = receipt.get("source_details", {}).get("tables", {}).get(name, {})
        profile = got.get("profile", {})
        if (not wanted or not got or got.get("schema") != fields
                or profile.get("schema_id") != facts.get("schema_id")
                or profile.get("snapshot_id") != facts.get("snapshot_id")
                or profile.get("table_uuid") != facts.get("table_uuid")
                or profile.get("row_count") != facts.get("row_count")
                or profile.get("row_count_basis") != facts.get("row_count_state")
                or profile.get("metadata_only") is not True or profile.get("profile_rows") != 0
                or profile.get("content_retained", False) is not False):
            mismatches.append(name)
    checks.append(_check("native_schema_identity_and_counts_match_audited_observation", not mismatches, mismatches))
    return checks


def grade_discovery(snapshot: dict[str, Any], receipt: dict[str, Any], *,
                    scope: str, provider: str = "example") -> list[dict[str, Any]]:
    """Compare observed assets and schema with the exact audit-backed receipt.

    Generic connector type normalization is checked explicitly; it cannot
    establish that source-specific types or Iceberg field IDs were preserved.
    """
    checks = [
        _check("receipt_scope_matches", receipt.get("scope") == scope),
        _check("receipt_has_observation_time", bool(receipt.get("observed_at"))),
        _check("discovery_complete", snapshot.get("status") == "discovery_complete"
               and snapshot.get("discovery_complete") is True),
        _check("meaning_uncertified", snapshot.get("meaning_certified") is False),
        _check("unpublished", snapshot.get("agent_ready") is False),
        _check("no_discovery_errors", not snapshot.get("errors")),
    ]
    raw = receipt.get("assets", [])
    actual = snapshot.get("assets", [])
    checks.append(_check("nonempty_observed_scope", bool(raw) and bool(actual), len(actual)))
    expected = {f"adapter/{provider}/{scope}/{item.get('pointer')}": item for item in raw}
    observed = {item.get("source_pointer"): item for item in actual}
    checks.append(_check("exact_asset_set", set(expected) == set(observed)
                         and len(expected) == len(raw) and len(observed) == len(actual),
                         {"expected": len(raw), "observed": len(actual)}))
    mismatches, revision_mismatches = [], []
    observation_mismatches = []
    version = snapshot.get("source", {}).get("protocol_version", "1.0")
    for pointer, wanted in expected.items():
        got = observed.get(pointer)
        expected_fields = [{"name": col.get("name"),
                            "data_type": type_name(col.get("native_type", col.get("data_type"))),
                            "native_type": col.get("native_type", col.get("data_type", "unknown")),
                            "nullable": col.get("nullable"),
                            **({"field_id": col["field_id"]} if version == "1.1" and "field_id" in col else {})}
                           for col in wanted.get("schema", [])]
        got_fields = (got or {}).get("schema", [])
        wanted_observations = {}
        if version == "1.1":
            wanted_observations = {"row_count": None, "row_count_basis": "unknown", **wanted.get("observations", {})}
        if (got or {}).get("observations", {}) != wanted_observations:
            observation_mismatches.append(pointer)
        if (not got or got.get("name") != wanted.get("name")
                or got_fields != expected_fields):
            mismatches.append(pointer)
        if not got or got.get("revision") != stable_id(
                "adapter_revision_schema", wanted.get("revision"), expected_fields,
                {key: value for key, value in wanted_observations.items() if key != "source_observed_at"}):
            revision_mismatches.append(pointer)
    checks.append(_check("exact_fields_nullable_and_normalized_types", not mismatches, mismatches))
    checks.append(_check("revision_bound_to_observed_schema", not revision_mismatches, revision_mismatches))
    checks.append(_check("exact_source_observations", not observation_mismatches, observation_mismatches))
    checks.append(_check("safe_source_pointers", all(
        isinstance(pointer, str) and "://" not in pointer and ".." not in pointer.split("/")
        for pointer in observed)))
    checks.append(_check("metadata_only_no_row_counts_invented", all(
        item.get("profile", {}).get("metadata_only") is True
        and item.get("profile", {}).get("row_count") == (
            expected[item["source_pointer"]].get("observations", {}).get("row_count") if version == "1.1" else None)
        and item.get("profile", {}).get("profile_rows", 0) == 0
        and item.get("profile", {}).get("content_retained") is False for item in actual)))
    handles = {item.get("id") for item in snapshot.get("evidence", []) if item.get("id")}
    checks.append(_check("nonempty_observed_evidence", bool(handles), len(handles)))
    field_handles = {(item.get("asset_id"), (item.get("details") or {}).get("field"))
                     for item in snapshot.get("evidence", []) if item.get("evidence_type") == "schema_and_profile"}
    checks.append(_check("all_discovered_fields_have_evidence", all(
        (asset.get("id"), field.get("name")) in field_handles
        for asset in actual for field in asset.get("schema", []))))
    unresolved = [item.get("id") for item in snapshot.get("proposals", [])
                  if not item.get("evidence_ids")
                  or not set(item["evidence_ids"]).issubset(handles)]
    checks.append(_check("proposals_have_current_evidence", not unresolved, unresolved))
    authoritative = [item.get("id") for key in ("concepts", "relationships", "metrics", "proposals")
                     for item in snapshot.get(key, [])
                     if item.get("status") in {"approved", "certified", "confirmed"}
                     or item.get("trust_state") == "owner_reviewed"]
    checks.append(_check("no_unreviewed_authority", not authoritative, authoritative))
    return checks


def _private_json_observation(path: Path) -> tuple[dict[str, Any], str]:
    raw = read_owner_only_file(str(path), max_bytes=2 * 1024 * 1024)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("Input receipt/profile must contain one JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _private_json(path: Path) -> dict[str, Any]:
    return _private_json_observation(path)[0]


def _write_report(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Report target must be an owned regular file with one link")
        os.fchmod(handle.fileno(), 0o600)
        handle.truncate(0)
        json.dump(value, handle, indent=2)
        handle.write("\n")


def _loopback(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("The pilot accepts loopback HTTP URLs only")
    return url.rstrip("/")


def _all_sources(client: httpx.Client) -> list[dict[str, Any]]:
    rows, cursor = [], None
    for _ in range(100):
        response = client.get("/api/knowledge/connections", params={
            "limit": 100, **({"cursor": cursor} if cursor else {})})
        response.raise_for_status()
        page = response.json()
        rows.extend(page["connections"])
        cursor = page.get("next_cursor")
        if not cursor:
            return rows
    raise ValueError("Source listing exceeded the pilot's bounded page budget")


def _connect(client: httpx.Client, existing: list[dict[str, Any]], *,
             name: str, kind: str, config: dict[str, Any]) -> str:
    for row in existing:
        if row.get("name") == name and row.get("kind") == kind:
            full = client.get(f"/api/knowledge/connections/{row['id']}")
            full.raise_for_status()
            saved = full.json()["connection"].get("config", {})
            if all(saved.get(key) == value for key, value in config.items()):
                return row["id"]
    response = client.post("/api/knowledge/connections", json={
        "name": name, "kind": kind, "config": config})
    response.raise_for_status()
    return response.json()["connection"]["id"]


def _request_measure(client: httpx.Client, path: str, *,
                     params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
    started = time.perf_counter()
    response = client.request(method, path, params=params)
    response.raise_for_status()
    return {"latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "payload_bytes": len(response.content), "result": response.json()}


def _question_probes(client: httpx.Client, connection_id: str,
                     receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Measure retrieval evidence; do not manufacture or grade a business answer."""
    fields = {col["name"] for item in receipt.get("assets", []) for col in item.get("schema", [])}
    questions = (
        ("Does sync success imply review approval?", "review approval",
         sorted(fields & {"status", "review_status", "sync_status"})),
        ("Which fields represent Forecast controls rather than realized outcomes?", "forecast",
         sorted(field for field in fields if "forecast" in field.casefold())),
        ("Are account names safe equivalence keys across providers?", "account identity",
         sorted(field for field in fields if any(part in field.casefold()
                                                for part in ("account_id", "crm_id", "canonical_id")))),
    )
    reports = []
    for question, query, relevant_fields in questions:
        source = _request_measure(client, f"/agent/api/sources/{connection_id}/evidence/search",
                                  params={"q": query, "limit": 5})
        docs = _request_measure(client, "/agent/api/document-evidence/search",
                                params={"q": query, "limit": 5})
        reports.append({"question": question, "query": query,
                        "observed_relevant_fields": relevant_fields,
                        "source_evidence": source, "document_evidence": docs,
                        "assessment": "retrieval_measured_semantic_answer_not_evaluated"})
    return reports


def _sample_observations(root: Path, relative: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compute independent aggregates over approved JSONL excerpts; retain no rows."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Sample JSON objects must not contain duplicate keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("Sample JSON numbers must be finite")

    def typed(value: Any) -> tuple[Any, ...]:
        # Decimal equality and hashing are exact without precision-context
        # normalization or expanding large exponents. Booleans and strings
        # remain different JSON types, including numeric-looking strings.
        if isinstance(value, bool):
            return ("boolean", value)
        if isinstance(value, (int, Decimal)):
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError("Sample JSON numbers must be finite")
            return ("number", number)
        if isinstance(value, str):
            return ("string", value)
        if isinstance(value, list):
            return ("array", tuple(typed(member) for member in value))
        if isinstance(value, dict):
            return ("object", tuple((key, typed(member)) for key, member in sorted(value.items())))
        if value is None:
            return ("null",)
        raise ValueError("Sample JSON values must use supported JSON types")

    def populated(value: Any) -> bool:
        return value is not None and not (isinstance(value, str) and not value.strip())

    def key_values(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> tuple[list[tuple[Any, ...]], bool]:
        values = []
        supported = True
        for row in rows:
            if not all(key in row and populated(row[key]) for key in keys):
                continue
            if any(isinstance(row[key], (dict, list)) for key in keys):
                supported = False
                continue
            values.append(tuple(typed(row[key]) for key in keys))
        return values, supported

    selected = (root / relative).resolve()
    if not selected.is_relative_to(root.resolve()) or not selected.is_dir():
        raise ValueError("Sample path must be a contained approved folder")
    paths = sorted(selected.glob("*.jsonl"))
    if not 1 <= len(paths) <= 10:
        raise ValueError("The sample drill requires 1-10 approved JSONL files")
    observations, memory_rows = {}, {}
    for path in paths:
        raw = read_owner_only_file(str(path), max_bytes=16 * 1024 * 1024)
        try:
            rows = [json.loads(line, object_pairs_hook=unique_object,
                               parse_float=Decimal, parse_constant=reject_constant)
                    for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
        except (UnicodeDecodeError, json.JSONDecodeError, ArithmeticError) as exc:
            raise ValueError("Sample JSONL must contain valid UTF-8 JSON numbers and objects") from exc
        if not rows or len(rows) > 5000 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Each sample file must contain 1-5000 JSON objects")
        pointer = (Path(relative) / path.name).as_posix()
        fields = sorted({field for row in rows for field in row})
        null_counts, missing_counts, empty_counts, distinct_counts = {}, {}, {}, {}
        for field in fields:
            present = [row[field] for row in rows if field in row]
            null_counts[field] = sum(value is None for value in present)
            missing_counts[field] = len(rows) - len(present)
            empty_counts[field] = sum(isinstance(value, str) and not value.strip() for value in present)
            # Empty strings are observed values for cardinality, even though
            # join probes exclude them. Typed keys never leave this function.
            distinct_counts[field] = len({typed(value) for value in present if value is not None})
        observations[pointer] = {"name": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                                 "rows": len(rows), "fields": fields,
                                 "null_counts": null_counts, "missing_counts": missing_counts,
                                 "empty_counts": empty_counts, "distinct_counts": distinct_counts,
                                 "completeness_scope": "entire_exported_file_only",
                                 "full_source_population_complete": False}
        memory_rows[pointer] = rows
    overlaps = []
    for source_pointer, source_rows in memory_rows.items():
        for target_pointer, target_rows in memory_rows.items():
            if source_pointer == target_pointer:
                continue
            for keys in (("account_id",), ("account_id", "reporting_year", "reporting_month")):
                if not all(set(keys).issubset(observations[pointer]["fields"])
                           for pointer in (source_pointer, target_pointer)):
                    continue
                source_values, source_supported = key_values(source_rows, keys)
                target_values, target_supported = key_values(target_rows, keys)
                if not source_values or not target_values:
                    continue
                target_keys = set(target_values)
                overlaps.append({"source_pointer": source_pointer, "target_pointer": target_pointer,
                                 "key_fields": list(keys),
                                 "source_nonnull_rows_probed": len(source_values),
                                 "matched_source_rows": sum(value in target_keys for value in source_values),
                                 "target_nonnull_rows_probed": len(target_values),
                                 "target_distinct_values_probed": len(target_keys),
                                 "target_unique_in_excerpt": len(target_keys) == len(target_values),
                                 "complete_source_and_target": source_supported and target_supported,
                                 "probe_completeness_scope": "exported_files_only",
                                 "full_source_population_cardinality": "unknown",
                                 "business_identity_verified": False, "raw_values_retained": False})
    return observations, overlaps


def grade_samples(snapshot: dict[str, Any], observations: dict[str, Any],
                  overlaps: list[dict[str, Any]]) -> dict[str, Any]:
    """Grade file-scoped counts separately from unsupported source-level joins."""
    assets = {asset.get("source_pointer"): asset for asset in snapshot.get("assets", [])}
    checks = [_check("exact_sample_asset_set", set(assets) == set(observations)
                     and len(assets) == len(snapshot.get("assets", []))),
              _check("sample_discovery_completed", snapshot.get("discovery_complete") is True
                     and not snapshot.get("errors")),
              _check("sample_unpublished_uncertified", snapshot.get("agent_ready") is False
                     and snapshot.get("meaning_certified") is False)]
    for pointer, expected in observations.items():
        asset = assets.get(pointer, {})
        profile = asset.get("profile", {})
        checks.extend([
            _check(f"file_revision:{pointer}", asset.get("revision") == expected["sha256"]),
            _check(f"file_row_count:{pointer}", profile.get("row_count") == expected["rows"]
                   and profile.get("profile_rows") == expected["rows"]
                   and profile.get("row_count_is_complete") is True
                   and profile.get("profile_is_sampled") is False),
            _check(f"field_names:{pointer}", sorted(col.get("name") for col in asset.get("schema", []))
                   == expected["fields"]),
            _check(f"null_counts:{pointer}", profile.get("null_counts") == expected["null_counts"]),
            _check(f"missing_counts:{pointer}", profile.get("missing_counts") == expected["missing_counts"]),
            _check(f"empty_counts:{pointer}", profile.get("empty_counts") == expected["empty_counts"]),
            _check(f"distinct_counts:{pointer}", profile.get("distinct_counts") == expected["distinct_counts"]
                   and not any(profile.get("distinct_counts_are_lower_bounds", {}).values())),
            _check(f"no_semantic_row_retention:{pointer}", profile.get("content_retained") is False
                   and not profile.get("semantic_samples")),
        ])
    metrics = snapshot.get("metrics", [])
    actual = [item for item in metrics if item.get("field") == "actual_revenue"]
    projected = [item for item in metrics if item.get("field") == "forecast_revenue"]
    metric_check = _check("actual_and_projected_metrics_remain_distinct_candidates",
                         bool(actual) and bool(projected)
                         and not {item["id"] for item in actual} & {item["id"] for item in projected}
                         and {item.get("source_asset_id") for item in actual}.isdisjoint(
                             {item.get("source_asset_id") for item in projected})
                         and all(item.get("status") == "candidate" and item.get("certified") is False
                                 for item in actual + projected))
    checks.append(_check("sample_ids_and_joins_not_self_approved", not any(
        item.get("status") in {"approved", "confirmed", "certified"}
        or item.get("trust_state") == "owner_reviewed"
        for key in ("concepts", "relationships", "metrics", "proposals")
        for item in snapshot.get(key, []))))
    evidence = [item for item in snapshot.get("evidence", [])
                if item.get("evidence_type") == "bounded_join_probe"]
    probe_checks = []
    for expected in overlaps:
        # Existing connector probes use one field. Composite overlap is an independent diagnostic only.
        if len(expected["key_fields"]) != 1:
            continue
        source_id = assets.get(expected["source_pointer"], {}).get("id")
        target_id = assets.get(expected["target_pointer"], {}).get("id")
        found = [item for item in evidence if item.get("details", {}).get("source_asset_id") == source_id
                 and item.get("details", {}).get("target_asset_id") == target_id
                 and item.get("details", {}).get("source_field") == expected["key_fields"][0]
                 and item.get("details", {}).get("target_field") == expected["key_fields"][0]]
        consistent = bool(found) and all(
            all(item["details"].get(key) == expected[key] for key in (
                "source_nonnull_rows_probed", "matched_source_rows", "target_nonnull_rows_probed",
                "target_distinct_values_probed", "complete_source_and_target"))
            and item["details"].get("target_unique_in_probe") == expected["target_unique_in_excerpt"]
            and item["details"].get("raw_values_retained") is False
            for item in found)
        probe_checks.append(_check(f"observed_join_overlap:{expected['source_pointer']}", consistent,
                                   {"platform_evidence_count": len(found),
                                    "independent_matched_rows": expected["matched_source_rows"]}))
    return {"checks": checks, "profiling_passed": all(item["passed"] for item in checks),
            "metric_candidate_checks": [metric_check],
            "metric_candidate_qualification_passed": metric_check["passed"],
            "metric_candidate_assessment": "domain_candidate_coverage_only",
            "metric_semantic_correctness_assessed": False,
            "join_probe_checks": probe_checks,
            "join_probe_qualification_passed": bool(probe_checks) and all(item["passed"] for item in probe_checks),
            "observations": observations, "independent_overlap_diagnostics": overlaps,
            "metric_candidates": [{key: item.get(key) for key in (
                "id", "name", "field", "source_asset_id", "status", "certified", "evidence_ids")}
                for item in actual + projected],
            "platform_join_probe_evidence_ids": [item.get("id") for item in evidence],
            "limits": ["Operator-selected records; source provenance, creation time and holdout status are not independently verified.",
                       "Complete file profiles do not prove full Iceberg table completeness or cardinality.",
                       "Identifier overlap is a file-scoped association, not verified business identity or join approval.",
                       "Metric separation is observed candidate structure, not a certified metric definition.",
                       "Absent automatic domain metric candidates is a coverage gap; external agents may submit evidence-backed typed contracts."]}


def _document_policy_probes(client: httpx.Client, *, root: Path, relative: str) -> dict[str, Any]:
    """Grade specific policy evidence recall and returned excerpt relevance.

    Exact selected documents are the observed corpus. Neither a matching filename
    nor an unrelated deployment identity-token comment answers an account identity question.
    """
    folder = (root / relative).resolve()
    if not folder.is_relative_to(root.resolve()):
        raise ValueError("Policy corpus must be inside the approved fixture root")
    policies = (
        ("vendor_projection_controls", "Forecast", "FORECAST_POLICY.md",
         ("forecast",), ("forecast",)),
        ("account_identity_crosswalk", "account identity", "IDENTITY_POLICY.md",
         ("account_resolver", "crm"), ("account_resolver", "crm", "name-only identity", "canonical identity crosswalk")),
        ("review_approval", "review approval", "REVIEW_POLICY.md",
         ("review approval",), ("review approval", "independent review", "approved review")),
    )
    corpus: dict[str, bytes] = {}

    def source_grounded(match: dict[str, Any]) -> bool:
        pointer = match.get("source_pointer")
        excerpt = match.get("excerpt")
        locator = match.get("source_locator")
        if not isinstance(pointer, str) or not isinstance(excerpt, str) or not excerpt or not isinstance(locator, dict):
            return False
        path = (root / pointer).resolve()
        if not path.is_relative_to(folder) or path.suffix != ".md":
            return False
        if pointer not in corpus:
            corpus[pointer] = read_owner_only_file(str(path), max_bytes=1024 * 1024)
        raw = corpus[pointer]
        lines = raw.decode("utf-8").splitlines(keepends=True)
        first, last = locator.get("line_start"), locator.get("line_end")
        left, right = locator.get("column_start"), locator.get("column_end")
        if (locator.get("format") != "md" or locator.get("coordinate_basis") != "source_lines"
                or not all(type(value) is int for value in (first, last, left, right))
                or not 1 <= first <= last <= len(lines) or left < 1 or right < 1
                or left > len(lines[first - 1]) or right > len(lines[last - 1])):
            return False
        if first == last:
            observed = lines[first - 1][left - 1:right]
        else:
            observed = (lines[first - 1][left - 1:] + "".join(lines[first:last - 1])
                        + lines[last - 1][:right])
        return observed == excerpt and match.get("source_revision") == hashlib.sha256(raw).hexdigest()

    reports = []
    for name, query, filename, expected_markers, supporting_markers in policies:
        path = folder / filename
        raw = read_owner_only_file(str(path), max_bytes=1024 * 1024)
        corpus[(Path(relative) / filename).as_posix()] = raw
        lines = raw.decode("utf-8").splitlines()
        relevant_lines = [index + 1 for index in range(len(lines))
                          if all(normalize_name(marker) in normalize_name(" ".join(lines[index:index + 2]))
                                 for marker in expected_markers)]
        measured = _request_measure(client, "/agent/api/document-evidence/search",
                                    params={"q": query, "limit": 5})
        matches = measured["result"].get("matches", [])
        supporting = [match for match in matches
                      if any(normalize_name(marker) in normalize_name(str(match.get("excerpt", "")))
                             for marker in supporting_markers)
                      and "identity token" not in normalize_name(str(match.get("excerpt", "")))]
        expected_support = [match for match in supporting
                            if str(match.get("source_pointer", "")).endswith(filename)]
        checks = [
            _check("copied_corpus_contains_expected_policy", bool(relevant_lines), relevant_lines),
            _check("expected_policy_evidence_recalled", bool(expected_support)),
            _check("returned_evidence_relevant", bool(matches) and len(supporting) == len(matches),
                   {"returned": len(matches), "supporting": len(supporting)}),
            _check("returned_excerpts_and_revisions_match_copied_source",
                   bool(matches) and all(source_grounded(match) for match in matches)),
        ]
        reports.append({"policy": name, "query": query,
                        "expected_document": (Path(relative) / filename).as_posix(),
                        "document_sha256": hashlib.sha256(raw).hexdigest(),
                        "checks": checks, "retrieval": measured,
                        "passed": all(item["passed"] for item in checks)})
    return {"policies": reports, "passed": all(item["passed"] for item in reports),
            "claim_scope": "Measured recall and relevance for three concrete copied policy passages; "
            "not overall enterprise corpus precision or generated-answer accuracy."}


def _negative_adapter(profile_file: Path | None, scope: str) -> dict[str, Any]:
    if profile_file is None:
        return {"status": "not_run", "reason": "No private adapter profile supplied"}
    profile = _private_json(profile_file)
    endpoint = _loopback(profile["endpoint"])
    bearer = profile.get("bearer_token")
    body = {"protocol_version": profile.get("protocol_version", "1.0"), "scope": scope, "max_assets": 100,
            "max_columns": 5000, "include_document_excerpts": False}
    cases = (
        ("missing_adapter_auth_denied", {}, body, {401, 403}),
        ("unknown_scope_denied", {"Authorization": f"Bearer {bearer}"},
         body | {"scope": "unapproved-synthetic-analytics-scope"}, {400, 403, 404, 422}),
        ("invalid_budget_denied", {"Authorization": f"Bearer {bearer}"},
         body | {"max_assets": 0}, {400, 422}),
    )
    checks = []
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for name, headers, data, allowed in cases:
            started = time.perf_counter()
            response = client.post(endpoint, headers=headers, json=data)
            checks.append(_check(name, response.status_code in allowed, {
                "status_code": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "payload_bytes": len(response.content)}))
    return {"status": "completed", "checks": checks,
            "passed": all(item["passed"] for item in checks)}


async def _mcp_probe(base_url: str, token: str, connection_ids: list[str]) -> dict[str, Any]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    measurements = []
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as client,
        streamable_http_client(f"{base_url}/agent/mcp/", http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        protocol = await session.initialize()
        tools = (await session.list_tools()).tools

        async def call(name: str, arguments: dict[str, Any]) -> None:
            started = time.perf_counter()
            result = await session.call_tool(name, arguments)
            content = result.structured_content
            measurements.append({"tool": name, "arguments": arguments,
                                 "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                                 "payload_bytes": len(json.dumps(content).encode()),
                                 "passed": not result.is_error and isinstance(content, dict),
                                 "result": content})

        await call("get_workspace", {"limit": 20})
        for connection_id in connection_ids:
            for track in ("meaning", "contract"):
                await call("next_work", {"connection_id": connection_id, "track": track, "limit": 2})
            await call("search_evidence", {"query": "status", "connection_id": connection_id,
                                           "scope": "source", "limit": 3})
        checks = [
            _check("mcp_real_calls_succeeded", all(item["passed"] for item in measurements)),
            _check("compact_tool_count", len(tools) <= 10, len(tools)),
            _check("workspace_payload_bounded", measurements[0]["payload_bytes"] < 24_000),
            _check("work_payload_bounded", all(item["payload_bytes"] < 12_000
                                               for item in measurements if item["tool"] == "next_work")),
        ]
        return {"protocol_version": protocol.protocol_version,
                "tool_count": len(tools),
                "schema_bytes": len(json.dumps([tool.model_dump(mode="json") for tool in tools]).encode()),
                "measurements": measurements, "checks": checks,
                "passed": all(item["passed"] for item in checks)}


def evaluate(args: argparse.Namespace, token: str) -> dict[str, Any]:
    report: dict[str, Any] = {"evaluated_at": datetime.now(UTC).isoformat(),
                             "base_url": args.base_url, "sources": [], "checks": [],
                             "claim_scope": "Observed read-only metadata integration, provenance and transport. "
                             "No semantic correctness, publication, data quality, workload, ACL or source write claim.",
                             "limits": ["Adapter receipts are observed discovery, not independent semantic truth.",
                                        "Native types and field IDs are checked as observed metadata; no business type semantics are inferred.",
                                        "No human review, publication, or source query execution is performed.",
                                        "HTTP and MCP timings are serial pilot observations, not production load results."]}
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=args.base_url, timeout=30, headers=headers) as client:
        existing = _all_sources(client)
        for scope in args.scopes:
            source_report: dict[str, Any] = {"scope": scope}
            report["sources"].append(source_report)
            try:
                connection_id = _connect(client, existing, name=f"Example pilot {scope}",
                                         kind="external_adapter", config={
                                             "secret_ref": args.secret_ref, "scope": scope,
                                             "max_assets": 100, "max_columns": 5000,
                                             "retain_semantic_samples": False})
                source_report["connection_id"] = connection_id
                refresh_started_at = datetime.now(UTC).isoformat()
                started = time.perf_counter()
                response = client.post(f"/api/knowledge/connections/{connection_id}/refresh")
                source_report["refresh_latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
                source_report["refresh_payload_bytes"] = len(response.content)
                response.raise_for_status()
                result = response.json()
                snapshot = result["snapshot"]
                connection = result["connection"]
                receipt_path = args.receipts / f"{scope}.json"
                receipt, receipt_digest = _private_json_observation(receipt_path)
                source_report.update({"snapshot_id": snapshot.get("id"),
                                      "snapshot_version": snapshot.get("version"),
                                      "receipt_sha256": receipt_digest,
                                      "receipt_observed_at": receipt.get("observed_at"),
                                      "source_details": receipt.get("source_details", {}),
                                      "asset_count": len(snapshot.get("assets", [])),
                                      "column_count": sum(len(item.get("schema", []))
                                                          for item in snapshot.get("assets", [])),
                                      "checks": grade_discovery(snapshot, receipt, scope=scope,
                                                                provider=args.adapter_provider)
                                                + grade_refresh(connection, receipt, refresh_started_at)})
                publication = _request_measure(client, f"/api/knowledge/connections/{connection_id}/publication")
                source_report["publication_preview"] = publication
                preview = publication["result"]
                source_report["checks"].append(_check(
                    "publication_requires_owner_review", preview.get("eligible") is False
                    and preview.get("reviewed_assertions") == 0
                    and "no_current_owner_review" in preview.get("blockers", [])))
                source_report["questions"] = _question_probes(client, connection_id, receipt)
                source_report["passed"] = all(item["passed"] for item in source_report["checks"])
                source_report["http_contract"] = _request_measure(
                    client, f"/agent/api/sources/{connection_id}/contract")
                source_report["http_search"] = _request_measure(
                    client, "/agent/api/search", params={"q": "account", "limit": 5})
                if snapshot.get("assets"):
                    source_report["http_graph"] = _request_measure(
                        client, "/agent/api/graph", params={"connection_id": connection_id,
                        "focus_id": snapshot["assets"][0]["id"], "depth": 1, "limit": 30})
            except (OSError, TypeError, ValueError, KeyError, httpx.HTTPError) as exc:
                # HTTP bodies and exception text can contain source details: retain only stable classes/status.
                source_report.update({"passed": False, "failure_type": type(exc).__name__})
                if isinstance(exc, httpx.HTTPStatusError):
                    source_report["status_code"] = exc.response.status_code
        if args.native_catalog:
            native = {"catalog": args.native_catalog, "namespace": args.native_namespace,
                      "tables": args.native_tables, "passed": False}
            report["native_iceberg"] = native
            try:
                if not any(item.get("passed") and item["scope"] == "local-iceberg" for item in report["sources"]):
                    raise ValueError("A current audited local Iceberg observation is required")
                native_id = _connect(client, existing, name="Example pilot native local Iceberg",
                                     kind="iceberg", config={"catalog": args.native_catalog,
                                     "namespace": args.native_namespace, "tables": args.native_tables})
                started_at = datetime.now(UTC).isoformat()
                measured = _request_measure(client, f"/api/knowledge/connections/{native_id}/refresh", method="POST")
                result = measured["result"]
                snapshot, connection = result["snapshot"], result["connection"]
                receipt = _private_json(args.receipts / "local-iceberg.json")
                checks = grade_native_iceberg(snapshot, receipt, catalog=args.native_catalog,
                                             namespace=args.native_namespace, tables=args.native_tables)
                checks += grade_connection_refresh(connection, started_at)
                native.update({"connection_id": native_id, "snapshot_id": snapshot.get("id"),
                               "asset_count": len(snapshot.get("assets", [])),
                               "column_count": sum(len(item.get("schema", [])) for item in snapshot.get("assets", [])),
                               "refresh_latency_ms": measured["latency_ms"], "checks": checks,
                               "passed": all(item["passed"] for item in checks)})
            except (OSError, TypeError, ValueError, KeyError, httpx.HTTPError) as exc:
                native["failure_type"] = type(exc).__name__
        if args.documents_path:
            doc_id = _connect(client, existing, name="Example pilot approved documents",
                              kind="local_files", config={"path": args.documents_path,
                              "include": "*.md", "retain_semantic_samples": True, "max_files": 100})
            started_at = datetime.now(UTC).isoformat()
            response = client.post(f"/api/knowledge/connections/{doc_id}/refresh")
            response.raise_for_status()
            result = response.json()
            snapshot = result["snapshot"]
            checks = grade_connection_refresh(result["connection"], started_at)
            report["documents"] = {"connection_id": doc_id, "snapshot_id": snapshot.get("id"),
                                   "asset_count": len(snapshot.get("assets", [])),
                                   "agent_ready": snapshot.get("agent_ready"),
                                   "checks": checks,
                                   "semantic_excerpts_retained": True,
                                   "passed": snapshot.get("status") == "discovery_complete"
                                   and snapshot.get("curation_complete") is True
                                   and bool(snapshot.get("assets"))
                                   and snapshot.get("agent_ready") is False
                                   and all(item["passed"] for item in checks)}
            # Repeat only retrieval after documents are discoverable; no additional source refresh.
            for source in report["sources"]:
                if source.get("passed"):
                    receipt = _private_json(args.receipts / f"{source['scope']}.json")
                    source["questions"] = _question_probes(client, source["connection_id"], receipt)
            report["document_policy_retrieval"] = _document_policy_probes(
                client, root=args.sample_root, relative=args.documents_path)
        if args.sample_path:
            sample_id = _connect(client, existing, name="Example bounded record fixture",
                                 kind="local_files", config={"path": args.sample_path,
                                 "include": "*.jsonl", "retain_semantic_samples": False,
                                 "profile_rows": 5000, "max_files": 10})
            started_at = datetime.now(UTC).isoformat()
            started = time.perf_counter()
            response = client.post(f"/api/knowledge/connections/{sample_id}/refresh")
            response.raise_for_status()
            result = response.json()
            snapshot = result["snapshot"]
            checks = grade_connection_refresh(result["connection"], started_at)
            observations, overlaps = _sample_observations(args.sample_root, args.sample_path)
            report["samples"] = grade_samples(snapshot, observations, overlaps) | {
                "connection_id": sample_id, "snapshot_id": snapshot.get("id"),
                "snapshot_version": snapshot.get("version"),
                "refresh_checks": checks,
                "refresh_latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "refresh_payload_bytes": len(response.content)}
            report["samples"]["http_contract"] = _request_measure(
                client, f"/agent/api/sources/{sample_id}/contract")
            report["samples"]["passed"] = report["samples"]["profiling_passed"] \
                and all(item["passed"] for item in checks) and snapshot.get("curation_complete") is True \
                and report["samples"]["join_probe_qualification_passed"] \
                and report["samples"]["metric_candidate_qualification_passed"]
        with httpx.Client(base_url=args.base_url, timeout=30) as anonymous:
            response = anonymous.get("/agent/api/sources")
            report["checks"].append(_check("missing_agent_auth_denied", response.status_code in {401, 403}, response.status_code))
            response = anonymous.get("/agent/mcp/")
            report["checks"].append(_check("missing_mcp_auth_denied", response.status_code in {401, 403}, response.status_code))
            response = anonymous.get("/agent/api/sources", headers={"Authorization": "Bearer invalid-pilot-token"})
            report["checks"].append(_check("invalid_agent_auth_denied", response.status_code in {401, 403}, response.status_code))
        response = client.get("/agent/api/sources/synthetic-analytics-nonexistent/status")
        report["checks"].append(_check("unknown_source_not_found", response.status_code == 404, response.status_code))
        response = client.get("/agent/api/contract-work", params={"connection_id": "unknown", "limit": 11})
        report["checks"].append(_check("oversized_work_request_denied", response.status_code == 422, response.status_code))
    report["adapter_negative_probes"] = _negative_adapter(args.adapter_profile_file, args.scopes[0])
    connection_ids = [row["connection_id"] for row in report["sources"] if row.get("passed")]
    if report.get("samples", {}).get("profiling_passed"):
        connection_ids.append(report["samples"]["connection_id"])
    try:
        report["mcp"] = asyncio.run(_mcp_probe(args.base_url, token, connection_ids))
    except Exception as exc:  # noqa: BLE001 -- MCP transports may raise SDK-specific exception groups; report failure.
        report["mcp"] = {"passed": False, "failure_type": type(exc).__name__}
    report["integration_passed"] = bool(report["sources"]) and all(row.get("passed") for row in report["sources"]) \
        and all(item["passed"] for item in report["checks"]) and report["mcp"]["passed"] \
        and report["adapter_negative_probes"].get("passed", True) \
        and report.get("native_iceberg", {}).get("passed", True) \
        and report.get("documents", {}).get("passed", True)
    report["passed"] = report["integration_passed"] \
        and report.get("samples", {}).get("passed", True) \
        and report.get("document_policy_retrieval", {}).get("passed", True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4173")
    parser.add_argument("--scopes", nargs="+", default=list(SCOPES))
    parser.add_argument("--receipts", type=Path, default=ROOT / ".local/synthetic-analytics-pilot/adapter-receipts")
    parser.add_argument("--adapter-profile-file", type=Path,
                        help="Owner-only adapter profile for direct denial probes; credentials never exported")
    parser.add_argument("--adapter-provider", default="example")
    parser.add_argument("--secret-ref", default="KNOWLEDGE_SOURCE_EXAMPLE")
    parser.add_argument("--native-catalog", help="Optional operator-provisioned local Iceberg catalog alias")
    parser.add_argument("--native-namespace", default="silver")
    parser.add_argument("--native-tables", nargs="+", default=["readings", "accounts"])
    parser.add_argument("--documents-path", help="Approved document folder relative to configured fixture root")
    parser.add_argument("--sample-path", help="Approved JSONL excerpt folder relative to the fixture root")
    parser.add_argument("--sample-root", type=Path, default=ROOT / ".local/synthetic-analytics-pilot/docs",
                        help="Approved local fixture root used for independent aggregate checks")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/synthetic-analytics-pilot/enterprise-drill.json")
    args = parser.parse_args()
    try:
        args.base_url = _loopback(args.base_url)
        token = os.environ.get("KNOWLEDGE_AGENT_TOKEN")
        if not token:
            parser.error("KNOWLEDGE_AGENT_TOKEN must be supplied through the process environment")
        report = evaluate(args, token)
        _write_report(args.output, report)
    except (OSError, TypeError, ValueError, httpx.HTTPError) as exc:
        report = {"evaluated_at": datetime.now(UTC).isoformat(), "passed": False,
                  "failure_type": type(exc).__name__, "claim_scope": "Pilot did not complete"}
        _write_report(args.output, report)
    print(json.dumps({"passed": report["passed"],
                      "scopes": [{"scope": item["scope"], "passed": item.get("passed"),
                                  "assets": item.get("asset_count"), "columns": item.get("column_count")}
                                 for item in report.get("sources", [])],
                      "mcp_passed": report.get("mcp", {}).get("passed"),
                      "native_iceberg_passed": report.get("native_iceberg", {}).get("passed"),
                      "sample_profiling_passed": report.get("samples", {}).get("profiling_passed"),
                      "sample_join_probe_qualification_passed": report.get("samples", {}).get("join_probe_qualification_passed"),
                      "sample_metric_candidate_qualification_passed": report.get("samples", {}).get("metric_candidate_qualification_passed"),
                      "document_policy_retrieval_passed": report.get("document_policy_retrieval", {}).get("passed"),
                      "integration_passed": report.get("integration_passed"),
                      "report": str(args.output)}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
