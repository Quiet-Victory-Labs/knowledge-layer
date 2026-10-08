"""Import bounded, operator-pinned portable catalog observations.

The package declares its coverage and provenance. Validation neither verifies a
vendor account nor compiles native semantics, interprets ACLs, or approves joins.
Complete listing and complete native field coverage are separate declarations.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import SAFE_SCOPE, decode_response
from knowledge_layer.inventory import _encoded, _records
from knowledge_layer.store import KnowledgeValidationError

SCHEMA_VERSION = "knowledge-catalog-export-v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000
_SCOPE_KEYS = {"provider", "source_identity", "namespace", "scope_id", "selectors"}
_OBSERVATION_KEYS = {"exported_at", "source_observed_at", "native_revision", "field_coverage", "redaction"}
_ENVELOPE_KEYS = {"schema_version", "scope", "observation", "listing_complete", "next_cursor", "record_count", "records"}
_RECORD_KEYS = {"native_id", "native_type", "kind", "name", "native_record"}
_KINDS = {"asset", "semantic_definition", "document", "relationship"}
_COVERAGE = {"complete", "partial", "unknown", "unavailable"}
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})\Z")


def _refuse(code: str = "catalog_export_invalid") -> ConnectorError:
    return ConnectorError(code, "The portable catalog export or its pinned scope is unavailable")


def _object(value: Any, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise _refuse()


def _text(value: Any, maximum: int) -> None:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip()
            or re.search(r"[\x00-\x1f\x7f]", value)):
        raise _refuse()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _refuse() from None


def _scope(value: Any) -> dict[str, Any]:
    try:
        _object(value, _SCOPE_KEYS)
        for key in ("provider", "scope_id"):
            if not isinstance(value[key], str) or not SAFE_SCOPE.fullmatch(value[key]):
                raise _refuse()
        for key in ("source_identity", "namespace"):
            _text(value[key], 120)
        selectors = value["selectors"]
        if not isinstance(selectors, list) or not 1 <= len(selectors) <= 200:
            raise _refuse()
        for selector in selectors:
            _text(selector, 256)
        if len(set(selectors)) != len(selectors):
            raise _refuse()
        return {**value, "selectors": sorted(selectors)}
    except ConnectorError:
        raise _refuse("catalog_scope_invalid") from None


def scope_digest(expected_scope: dict[str, Any]) -> str:
    """Digest exact pinned identity/coverage; only selector order is canonicalized."""
    return hashlib.sha256(_encoded(_scope(expected_scope))).hexdigest()


def _clock(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 40 or not _CLOCK.fullmatch(value):
        raise _refuse()
    try:
        if datetime.fromisoformat(value).utcoffset() is None:
            raise _refuse()
    except ValueError:
        raise _refuse() from None


def _observation(value: Any) -> None:
    _object(value, _OBSERVATION_KEYS)
    for key in ("exported_at", "source_observed_at"):
        _clock(value[key])
    revision = value["native_revision"]
    if not (revision is None or type(revision) is int and -(2**63) <= revision < 2**63
            or isinstance(revision, str) and len(revision) <= 512):
        raise _refuse()
    coverage = value["field_coverage"]
    if not isinstance(coverage, dict) or not 1 <= len(coverage) <= 32:
        raise _refuse()
    for field, status in coverage.items():
        _text(field, 120)
        if not isinstance(status, str) or status not in _COVERAGE:
            raise _refuse()
    if not isinstance(value["redaction"], str) or value["redaction"] not in {"none", "partial", "unknown"}:
        raise _refuse()


def _declarations(value):
    _observation(value["observation"])
    if value["listing_complete"] is not True or value["next_cursor"] is not None:
        raise _refuse("catalog_export_incomplete")
    count = value["record_count"]
    if type(count) is not int or not 0 <= count <= MAX_RECORDS:
        raise _refuse()


def validate_contract(value):
    """Check producer declarations, without certifying the native extraction."""
    _object(value, _ENVELOPE_KEYS - {"schema_version", "records"})
    _scope(value["scope"])
    _declarations(value)
    return deepcopy(value)


def inventory_records(raw: bytes, *, expected_scope: dict[str, Any]) -> dict[str, Any]:
    """Return complete captured inventory records, or refuse the entire package.

    Native records and declared provenance stay exact, including null, zero,
    empty and missing values. A complete listing can declare partial field
    coverage/redaction; that is not complete source metadata or approved meaning.
    The watermark binds artifact bytes and pinned scope, not freshness or ACLs.
    This pure import performs no network, source writes, models, or compilation.
    """
    pinned = _scope(expected_scope)
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise _refuse("catalog_export_budget_exceeded")
    try:
        package = decode_response(raw, max_bytes=MAX_BYTES)
    except ConnectorError:
        raise _refuse() from None
    _object(package, _ENVELOPE_KEYS)
    if package["schema_version"] != SCHEMA_VERSION:
        raise _refuse()
    declared = _scope(package["scope"])
    if declared != pinned:
        raise _refuse("catalog_scope_mismatch")
    _declarations(package)
    records, count = package["records"], package["record_count"]
    if not isinstance(records, list) or len(records) != count:
        raise _refuse()
    indexed = {}
    for record in records:
        _object(record, _RECORD_KEYS)
        _text(record["native_id"], 512)
        _text(record["native_type"], 120)
        if (not isinstance(record["kind"], str) or record["kind"] not in _KINDS
                or not isinstance(record["native_record"], dict)
                or record["name"] is not None
                and (not isinstance(record["name"], str) or len(record["name"]) > 512)):
            raise _refuse()
        key = (declared["provider"], declared["source_identity"], declared["namespace"],
               record["native_type"], record["native_id"])
        if key in indexed:
            raise _refuse()
        indexed[key] = record
    entities, evidence, capture_bytes = [], [], 0
    try:
        for identity in sorted(indexed):
            record = indexed[identity]
            digest = hashlib.sha256(_encoded(identity)).hexdigest()
            entity_id, evidence_id = "catalog_entity_" + digest, "catalog_evidence_" + digest
            entity = {"id": entity_id, "kind": record["kind"], "evidence_ids": [evidence_id], "payload": {
                "native_id": record["native_id"], "native_type": record["native_type"], "name": record["name"],
                "source": deepcopy(package["scope"]),
                "revision": hashlib.sha256(_encoded(record["native_record"])).hexdigest(),
                "observation": deepcopy(package["observation"]), "locally_executable": False,
                "source_clock_state": "unknown" if package["observation"]["source_observed_at"] is None else "declared",
            }}
            proof = {"id": evidence_id, "kind": "catalog_native_record", "payload": {
                "native_record": record["native_record"], "scope": deepcopy(package["scope"]),
                "observation": deepcopy(package["observation"]),
            }}
            _records([entity])
            _records([proof], evidence=True)
            capture_bytes += len(_encoded(entity)) + len(_encoded(proof))
            if capture_bytes > MAX_BYTES:
                raise _refuse("catalog_export_budget_exceeded")
            entities.append(entity)
            evidence.append(proof)
        hasher = hashlib.sha256()
        hasher.update(len(raw).to_bytes(8, "big"))
        hasher.update(raw)
        hasher.update(_encoded(pinned))
        result = {"entities": entities, "evidence": evidence,
                  "watermark": {"kind": "catalog_export_content_sha256", "value": hasher.hexdigest()}}
        if len(_encoded(result)) > MAX_BYTES:
            raise _refuse("catalog_export_budget_exceeded")
        return result
    except KnowledgeValidationError:
        raise _refuse("catalog_export_budget_exceeded") from None
