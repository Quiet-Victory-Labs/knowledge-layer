"""Read bounded dbt manifest v12 semantic records without compiling SQL.

Raw node semantics remain opaque upstream evidence. Ingestion never converts a
dbt metric into a locally executable metric or grants it publication authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import (
    _upstream_definition,
    decode_response,
)

SCHEMA = "https://schemas.getdbt.com/dbt/manifest/v12.json"
MAX_BYTES = 2 * 1024 * 1024
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
MAX_INVENTORY_SELECTION = 10_000
MAX_INVENTORY_RECORD_BYTES = 64 * 1024
_ARTIFACT_METADATA = ("dbt_schema_version", "generated_at", "project_name", "dbt_version", "invocation_id")
_COLLECTIONS = (("metrics", "metric"), ("semantic_models", "semantic_model"), ("exposures", "exposure"))


def import_manifest(raw: bytes, *, namespace: str, unique_ids: list[str]) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise ConnectorError("artifact_size_exceeded", "dbt artifact exceeds the 2 MiB import budget")
    try:
        manifest = decode_response(raw, max_bytes=MAX_BYTES)
    except ConnectorError:
        raise ConnectorError("artifact_invalid", "dbt artifact must be unique-key finite UTF-8 JSON") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("metadata"), dict):
        raise ConnectorError("artifact_invalid", "dbt artifact requires native metadata")
    metadata = manifest["metadata"]
    if metadata.get("dbt_schema_version") != SCHEMA:
        raise ConnectorError("artifact_schema_unsupported", "Only dbt manifest v12 is supported by this importer")
    observed = metadata.get("generated_at")
    try:
        stamp = datetime.fromisoformat(observed)
        if stamp.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ConnectorError("artifact_clock_unknown", "dbt artifact needs its aware native generation clock") from exc
    if (not isinstance(unique_ids, list) or not 1 <= len(unique_ids) <= 100
            or any(not isinstance(item, str) for item in unique_ids) or len(set(unique_ids)) != len(unique_ids)):
        raise ConnectorError("artifact_scope_invalid", "Select 1-100 distinct native dbt unique IDs")
    records = {}
    for collection, kind in (("semantic_models", "semantic_model"), ("metrics", "metric"), ("exposures", "data_product")):
        values = manifest.get(collection, {})
        if not isinstance(values, dict):
            raise ConnectorError("artifact_invalid", "dbt semantic collections must use native ID mappings")
        for key in unique_ids:
            if key in values:
                if key in records:
                    raise ConnectorError("artifact_invalid", "dbt artifact repeats a selected native identity")
                records[key] = kind, values[key]
    if set(records) != set(unique_ids):
        raise ConnectorError("artifact_scope_incomplete", "A selected dbt semantic record is absent; removal is not established")
    assets = []
    for key in sorted(records):
        kind, node = records[key]
        if not isinstance(node, dict) or node.get("unique_id") != key or not isinstance(node.get("name"), str):
            raise ConnectorError("artifact_invalid", "dbt node must preserve its exact native unique ID and name")
        native = json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(native.encode()) > 4000:
            raise ConnectorError("artifact_record_too_large", "dbt semantic record exceeds the opaque definition budget; use a narrower native adapter")
        digest = hashlib.sha256(native.encode()).hexdigest()
        definition = node.get("description") or native
        owner = node.get("owner")
        owner = owner.get("name") if isinstance(owner, dict) else None
        upstream = {"id": key, "kind": kind, "namespace": namespace, "definition": definition,
                    "version": digest, "source_observed_at": observed, "formula": native,
                    "authority": "dbt", "owner": owner, "review_status": None,
                    "reference": "dbt/" + key}
        # Validate the same public wire contract used by independent adapters.
        _upstream_definition(upstream, node["name"])
        assets.append({"name": node["name"], "pointer": "dbt/" + key,
                       "asset_type": "semantic_definition", "revision": digest,
                       "schema": [], "upstream_definition": upstream})
    return {"protocol_version": "1.2", "listing_complete": True, "next_cursor": None,
            "assets": assets, "import_scope": "explicit_native_ids", "artifact_schema": SCHEMA,
            "native_execution": "delegated_to_customer_dbt_runtime", "locally_executable": False}


def _inventory_text(value: Any, maximum: int, *, scope: bool = False) -> None:
    try:
        valid = (isinstance(value, str) and 1 <= len(value) <= maximum and value.strip()
                 and re.search(r"[\x00-\x1f\x7f]", value) is None)
        if not valid:
            raise ValueError
        value.encode("utf-8")
    except (ValueError, UnicodeError):
        raise ConnectorError("artifact_scope_invalid" if scope else "artifact_invalid",
                             "dbt inventory text must be bounded nonblank Unicode") from None


def _inventory_encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ConnectorError("artifact_invalid", "dbt inventory content must be finite Unicode JSON") from None


def _inventory_clock(metadata: dict[str, Any]) -> str | None:
    observed = metadata.get("generated_at")
    if observed is None:
        return None
    try:
        if not isinstance(observed, str) or datetime.fromisoformat(observed).utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ConnectorError("artifact_clock_unknown", "dbt generation clock must be aware when provided") from None
    return observed


def inventory_records(raw: bytes, *, namespace: str, unique_ids: list[str]) -> dict[str, Any]:
    """Preserve complete, selected v12 records for the paged inventory path.

    This validates the version, exact identities, JSON integrity, and finite
    budgets, not the entire dbt schema or a customer's compiled semantics.
    Unknown native fields and dependencies stay opaque in evidence. All
    requested IDs must resolve; no partial listing establishes removal.
    The content watermark binds exact artifact bytes and selected scope, not
    source freshness. No model, SQL, dependency expansion, or network runs.
    """
    _inventory_text(namespace, 120, scope=True)
    if (not isinstance(unique_ids, list) or not 1 <= len(unique_ids) <= MAX_INVENTORY_SELECTION):
        raise ConnectorError("artifact_scope_invalid", "Select 1-10000 distinct exact native dbt IDs")
    for identifier in unique_ids:
        _inventory_text(identifier, 256, scope=True)
    if len(set(unique_ids)) != len(unique_ids):
        raise ConnectorError("artifact_scope_invalid", "dbt inventory selection must not repeat native IDs")
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_INVENTORY_BYTES:
        raise ConnectorError("artifact_size_exceeded", "dbt artifact exceeds the 16 MiB inventory budget")
    try:
        manifest = decode_response(raw, max_bytes=MAX_INVENTORY_BYTES)
    except ConnectorError:
        raise ConnectorError("artifact_invalid", "dbt artifact must be unique-key finite UTF-8 JSON") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("metadata"), dict):
        raise ConnectorError("artifact_invalid", "dbt artifact requires native metadata")
    metadata = manifest["metadata"]
    if metadata.get("dbt_schema_version") != SCHEMA:
        raise ConnectorError("artifact_schema_unsupported", "Only dbt manifest v12 supports this inventory import")
    observed = _inventory_clock(metadata)
    artifact_metadata = {key: metadata[key] for key in _ARTIFACT_METADATA if key in metadata}
    if any(value is not None and not isinstance(value, str) for value in artifact_metadata.values()):
        raise ConnectorError("artifact_invalid", "Selected dbt artifact metadata must use native strings or null")
    records = {}
    for collection, kind in _COLLECTIONS:
        if collection not in manifest:
            continue
        values = manifest[collection]
        if not isinstance(values, dict):
            raise ConnectorError("artifact_invalid", "dbt semantic collections must use native ID mappings")
        for identifier in unique_ids:
            if identifier in values:
                if identifier in records:
                    raise ConnectorError("artifact_invalid", "dbt artifact repeats a selected native identity")
                records[identifier] = kind, values[identifier]
    if set(records) != set(unique_ids):
        raise ConnectorError("artifact_scope_incomplete", "A selected native dbt record is absent; removal is not established")
    entities, evidence = [], []
    for identifier in sorted(records):
        kind, node = records[identifier]
        if (not isinstance(node, dict) or node.get("unique_id") != identifier
                or ("resource_type" in node and node["resource_type"] != kind)):
            raise ConnectorError("artifact_invalid", "dbt inventory node must match its native identity and collection")
        _inventory_text(node.get("name"), MAX_INVENTORY_RECORD_BYTES)
        native_digest = hashlib.sha256(_inventory_encoded(node)).hexdigest()
        scoped_digest = hashlib.sha256(_inventory_encoded([namespace, identifier])).hexdigest()
        entity_id, evidence_id = "dbt_entity_" + scoped_digest, "dbt_evidence_" + scoped_digest
        item = {"id": entity_id, "kind": "semantic_definition", "evidence_ids": [evidence_id], "payload": {
            "native_id": identifier, "name": node["name"], "namespace": namespace, "authority": "dbt",
            "native_kind": kind, "revision": native_digest, "source_observed_at": observed,
            "source_clock_state": "unknown" if observed is None else "known", "locally_executable": False,
            "native_execution": "delegated_customer_dbt_runtime",
        }}
        proof = {"id": evidence_id, "kind": "upstream_semantic_definition",
                 "payload": {"native_record": node, "artifact_metadata": artifact_metadata}}
        if any(len(_inventory_encoded(value)) > MAX_INVENTORY_RECORD_BYTES for value in (item, proof)):
            raise ConnectorError("artifact_record_too_large", "Complete dbt inventory record exceeds 64 KiB; narrow its source definition")
        entities.append(item)
        evidence.append(proof)
    scope = _inventory_encoded({"namespace": namespace, "unique_ids": sorted(unique_ids)})
    watermark = hashlib.sha256()
    # Length framing prevents an artifact/scope boundary collision without
    # interpreting an artifact clock or normalizing the exact input bytes.
    watermark.update(len(raw).to_bytes(8, "big"))
    watermark.update(raw)
    watermark.update(scope)
    return {"entities": entities, "evidence": evidence,
            "watermark": {"kind": "content_sha256", "value": watermark.hexdigest()}}
