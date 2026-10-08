"""Versioned HTTPS boundary for separately deployed enterprise source adapters."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from knowledge_layer.connectors.common import ConnectorError, stable_id, type_name
from knowledge_layer.connectors.secrets import read_source_secret

PROTOCOL_VERSION = "1.0"
SUPPORTED_PROTOCOL_VERSIONS = {"1.0", "1.1", "1.2"}
SUPPORTED_PROFILE_PROTOCOLS = SUPPORTED_PROTOCOL_VERSIONS | {"1.3"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ASSETS = 100
MAX_COLUMNS = 5_000
MAX_DOCUMENT_TERMS = 50
UPSTREAM_DEFINITION_KINDS = {"catalog_asset", "glossary_term", "ontology_term", "metric", "semantic_model", "data_product"}
SAFE_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
SAFE_POINTER = re.compile(r"^[A-Za-z0-9_.:/@#-]{1,256}$")
SAFE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
SAFE_REFERENCE = re.compile(r"^KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}$")


def _profile(secret_ref: str, scope: str) -> dict[str, Any]:
    raw = read_source_secret(secret_ref, unavailable_code="adapter_profile_unavailable")
    try:
        profile = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectorError("adapter_profile_invalid", "The configured adapter profile is invalid") from exc
    return validate_profile(profile, scope=scope)


def validate_profile(profile: Any, *, scope: str) -> dict[str, Any]:
    """Validate public profile fields without reading credentials or contacting a source."""
    if not isinstance(profile, dict):
        raise ConnectorError("adapter_profile_invalid", "The configured adapter profile is invalid")
    endpoint = profile.get("endpoint")
    try:
        parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
    except ValueError as exc:
        raise ConnectorError("adapter_profile_invalid", "The configured adapter endpoint is invalid") from exc
    if parsed is None or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/discover":
        raise ConnectorError("adapter_profile_invalid", "The configured adapter endpoint is invalid")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and os.environ.get("KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS") == "1"):
        raise ConnectorError("adapter_tls_required", "The configured adapter endpoint must use HTTPS")
    provider = profile.get("provider")
    allowed_scopes = profile.get("allowed_scopes")
    if not isinstance(provider, str) or not SAFE_SCOPE.fullmatch(provider):
        raise ConnectorError("adapter_profile_invalid", "The configured adapter profile is invalid")
    if not isinstance(allowed_scopes, list) or not allowed_scopes or len(allowed_scopes) > 100 or any(not isinstance(item, str) or not SAFE_SCOPE.fullmatch(item) for item in allowed_scopes):
        raise ConnectorError("adapter_profile_invalid", "The configured adapter profile is invalid")
    if scope not in allowed_scopes:
        raise ConnectorError("adapter_scope_denied", "The selected scope is not allowed by this adapter reference")
    bearer_token = profile.get("bearer_token")
    if bearer_token is not None and (not isinstance(bearer_token, str) or not bearer_token):
        raise ConnectorError("adapter_profile_invalid", "The configured adapter profile is invalid")
    version = profile.get("protocol_version", PROTOCOL_VERSION)
    if not isinstance(version, str) or version not in SUPPORTED_PROFILE_PROTOCOLS:
        raise ConnectorError("adapter_profile_invalid", "The configured adapter protocol is unsupported")
    if version == "1.3":
        for field in ("revision", "source_identity"):
            if (not isinstance(profile.get(field), str) or not 1 <= len(profile[field]) <= 120
                    or not profile[field].strip() or re.search(r"[\x00-\x1f\x7f]", profile[field])):
                raise ConnectorError("adapter_profile_invalid", "Paged profiles require operator identity and revision")
        bindings = profile.get("inventory_scope_bindings")
        if "inventory_scope_bindings" in profile and (not isinstance(bindings, dict) or not 1 <= len(bindings) <= 100
                or any(key not in allowed_scopes or not isinstance(value, str)
                       or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in bindings.items())):
            raise ConnectorError("adapter_profile_invalid", "Inventory scope bindings must pin approved scopes")
    return profile


def _metadata_string(value: Any, field: str, limit: int = 120) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or not SAFE_NAME.fullmatch(value[:120]):
        raise ConnectorError("adapter_response_invalid", f"The adapter returned an invalid {field}")
    return value


def validate_asset_names(value: Any) -> list[str]:
    """Validate an exact, bounded local selection without rewriting source names."""
    if (not isinstance(value, list) or not 1 <= len(value) <= MAX_ASSETS
            or any(not isinstance(name, str) or not SAFE_NAME.fullmatch(name) or not name.strip()
                   for name in value)
            or len(set(value)) != len(value)):
        raise ConnectorError(
            "invalid_asset_selection",
            "asset_names must contain 1-100 unique nonblank exact asset names of at most 120 characters",
        )
    return sorted(value)


def _native_type(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid native type")
    return value


def _count(value: Any, field: str) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise ConnectorError("adapter_response_invalid", f"The adapter returned an invalid {field}")
    return value


def _response_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConnectorError("adapter_response_invalid", "The adapter returned duplicate metadata keys")
        result[key] = value
    return result


def _observations(item: dict[str, Any], columns: set[str]) -> dict[str, Any]:
    """Validate optional v1.1 facts without converting absence to zero or truth."""
    raw = item.get("observations", {})
    if not isinstance(raw, dict) or set(raw) - {
        "schema_id", "snapshot_id", "schema_fingerprint", "source_observed_at",
        "row_count", "row_count_basis", "primary_key", "foreign_keys", "lineage",
    }:
        raise ConnectorError("adapter_response_invalid", "The adapter returned unsupported observations")
    result: dict[str, Any] = {}
    for key in ("schema_id", "snapshot_id"):
        if key in raw:
            result[key] = _count(raw[key], key)
    if "schema_fingerprint" in raw:
        value = raw["schema_fingerprint"]
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid schema fingerprint")
        result["schema_fingerprint"] = value
    if "source_observed_at" in raw:
        value = raw["source_observed_at"]
        try:
            timestamp = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 40 else None
            if timestamp is None or timestamp.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid observation clock") from None
        result["source_observed_at"] = value
    count, basis = raw.get("row_count"), raw.get("row_count_basis", "unknown")
    if not isinstance(basis, str) or basis not in {"unknown", "exact", "estimate", "snapshot_summary"}:
        raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid row-count basis")
    count = _count(count, "row count")
    if (count is None) != (basis == "unknown"):
        raise ConnectorError("adapter_response_invalid", "The adapter returned contradictory row-count evidence")
    result.update(row_count=count, row_count_basis=basis)

    def fields(value: Any, *, source: bool) -> list[str]:
        if not isinstance(value, list) or not 1 <= len(value) <= 32:
            raise ConnectorError("adapter_response_invalid", "The adapter returned invalid key fields")
        names = [_metadata_string(name, "key field") for name in value]
        if len(set(names)) != len(names) or (source and not set(names) <= columns):
            raise ConnectorError("adapter_response_invalid", "The adapter returned unknown or duplicate key fields")
        return names

    if "primary_key" in raw:
        result["primary_key"] = fields(raw["primary_key"], source=True) if raw["primary_key"] not in (None, []) else raw["primary_key"]
    if "foreign_keys" in raw:
        keys = raw["foreign_keys"]
        if not isinstance(keys, list) or len(keys) > 50:
            raise ConnectorError("adapter_response_invalid", "The adapter returned invalid foreign keys")
        result["foreign_keys"] = []
        for key in keys:
            if not isinstance(key, dict) or set(key) != {"columns", "target_pointer", "target_columns"}:
                raise ConnectorError("adapter_response_invalid", "The adapter returned invalid foreign-key metadata")
            source_fields, target_fields = fields(key["columns"], source=True), fields(key["target_columns"], source=False)
            pointer = key["target_pointer"]
            if len(source_fields) != len(target_fields) or not _valid_pointer(pointer):
                raise ConnectorError("adapter_response_invalid", "The adapter returned invalid foreign-key metadata")
            result["foreign_keys"].append({"columns": source_fields, "target_pointer": pointer, "target_columns": target_fields})
    if "lineage" in raw:
        lineage = raw["lineage"]
        if not isinstance(lineage, list) or len(lineage) > 50 or any(not _valid_pointer(value) for value in lineage):
            raise ConnectorError("adapter_response_invalid", "The adapter returned invalid lineage pointers")
        result["lineage"] = list(dict.fromkeys(lineage))
    return result


def _valid_pointer(value: Any) -> bool:
    return isinstance(value, str) and bool(SAFE_POINTER.fullmatch(value)) and "://" not in value and ".." not in value.split("/")


def _upstream_definition(value: Any, name: str) -> dict[str, Any]:
    """Retain bounded source assertions without granting local meaning authority."""
    required = {"id", "kind", "namespace", "definition", "version"}
    optional = {"source_observed_at", "owner", "authority", "reference", "review_status", "unit", "formula"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ConnectorError("adapter_response_invalid", "The adapter returned invalid upstream definition fields")

    def text(key: str, limit: int, *, multiline: bool = False) -> str:
        raw = value[key]
        controls = r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]" if multiline else r"[\x00-\x1f\x7f]"
        if not isinstance(raw, str) or not raw.strip() or len(raw) > limit or re.search(controls, raw):
            raise ConnectorError("adapter_response_invalid", f"The adapter returned an invalid upstream {key}")
        return raw

    kind = value["kind"]
    if not isinstance(kind, str) or kind not in UPSTREAM_DEFINITION_KINDS:
        raise ConnectorError("adapter_response_invalid", "The adapter returned an unsupported upstream definition kind")
    result = {"id": text("id", 256), "kind": kind, "name": name,
              "namespace": text("namespace", 120), "definition": text("definition", 4000, multiline=True),
              "version": text("version", 128) if value["version"] is not None else None}
    for key in ("owner", "authority", "review_status", "unit"):
        result[key] = text(key, 120) if value.get(key) is not None else None
    result["formula"] = text("formula", 4000, multiline=True) if value.get("formula") is not None else None
    reference = value.get("reference")
    if reference is not None:
        reference = text("reference", 256)
        try:
            parsed = urlparse(reference)
        except ValueError:
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid upstream reference") from None
        if not _valid_pointer(reference) and not (
            parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        ):
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid upstream reference")
    result["reference"] = reference
    clock = value.get("source_observed_at")
    if clock is not None:
        try:
            timestamp = datetime.fromisoformat(clock) if isinstance(clock, str) and len(clock) <= 40 else None
            if timestamp is None or timestamp.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid upstream observation clock") from None
    result["source_observed_at"] = clock
    return result


def _document_locator(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "format", "coordinate_basis", "line_start", "line_end", "column_start", "column_end",
        "extracted_line_start", "extracted_line_end", "source_line_start", "source_line_end",
        "source_node_line_start", "source_node_line_end",
        "page", "page_start", "page_end", "paragraph", "paragraph_start", "paragraph_end",
    }:
        raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document locator")
    if not isinstance(value.get("format"), str) or value["format"] not in {"md", "txt", "markdown", "text", "html", "htm", "pdf", "docx"}:
        raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document format")
    for key, number in value.items():
        if key == "coordinate_basis":
            _metadata_string(number, "document coordinate basis")
        elif key != "format" and (type(number) is not int or not (0 if key.startswith("column") else 1) <= number <= 1000000):
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document locator")
    for start, end in (("line_start", "line_end"), ("page_start", "page_end"),
                       ("paragraph_start", "paragraph_end"), ("source_line_start", "source_line_end"),
                       ("source_node_line_start", "source_node_line_end"),
                       ("extracted_line_start", "extracted_line_end")):
        if start in value and end in value and value[start] > value[end]:
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document range")
    # Multi-line chunks may end before their first-line column. Check column
    # ordering only when the locator explicitly identifies a single line.
    line_pair = next(((value[start], value[end]) for start, end in (
        ("extracted_line_start", "extracted_line_end"), ("line_start", "line_end"),
        ("source_line_start", "source_line_end"), ("source_node_line_start", "source_node_line_end"),
    ) if start in value and end in value), None)
    if (line_pair is not None and line_pair[0] == line_pair[1]
            and value.get("column_start", 0) > value.get("column_end", 1000000)):
        raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document range")
    return value


def _scan_settings(config: dict[str, Any]) -> tuple[list[str] | None, int, int, bool]:
    asset_names = validate_asset_names(config["asset_names"]) if "asset_names" in config else None
    max_assets = config.get("max_assets", MAX_ASSETS)
    max_columns = config.get("max_columns", MAX_COLUMNS)
    retain_samples = config.get("retain_semantic_samples", False)
    if not isinstance(retain_samples, bool):
        raise ConnectorError("invalid_sample_policy", "retain_semantic_samples must be a boolean")
    if type(max_assets) is not int or not 1 <= max_assets <= MAX_ASSETS:
        raise ConnectorError("invalid_scan_limit", "max_assets must be between 1 and 100")
    if type(max_columns) is not int or not 1 <= max_columns <= MAX_COLUMNS:
        raise ConnectorError("invalid_scan_limit", "max_columns must be between 1 and 5000")
    return asset_names, max_assets, max_columns, retain_samples


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON numbers are unsupported")
    return number


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("Nonfinite JSON numbers are unsupported")


def _validate_json_strings(payload: Any) -> None:
    """Reject escaped lone surrogates throughout the parsed JSON, including keys."""
    pending = [iter((payload,))]
    try:
        while pending:
            try:
                value = next(pending[-1])
            except StopIteration:
                pending.pop()
                continue
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, dict):
                for key in value:
                    key.encode("utf-8")
                pending.append(iter(value.values()))
            elif isinstance(value, list):
                pending.append(iter(value))
    except UnicodeError:
        raise ConnectorError("adapter_response_invalid", "The adapter returned invalid Unicode metadata") from None


def decode_response(raw: bytes, *, max_bytes: int = MAX_RESPONSE_BYTES) -> Any:
    """Shared bounded, unique-key, finite UTF-8 parser for adapter envelopes."""
    if not isinstance(raw, bytes):
        raise ConnectorError("adapter_response_invalid", "The adapter response must be UTF-8 JSON bytes")
    if len(raw) > max_bytes:
        raise ConnectorError("adapter_response_too_large", "The adapter metadata response exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_response_object,
                             parse_float=_finite_float, parse_constant=_invalid_json_constant)
    except ConnectorError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectorError("adapter_unavailable", "The external source adapter did not return valid metadata") from None
    _validate_json_strings(payload)
    return payload


def normalize_response(
    raw: bytes,
    *,
    provider: str,
    scope: str,
    protocol_version: str = PROTOCOL_VERSION,
    max_assets: int = MAX_ASSETS,
    max_columns: int = MAX_COLUMNS,
    retain_semantic_samples: bool = False,
    asset_names: list[str] | None = None,
    source_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Validate complete response bytes without credentials, network or source IO.

    This is the discovery normalizer, not a source approval or vendor qualification.
    Provider/scope are the request's locally selected identity; response bytes cannot
    independently prove that the adapter read that source or honored its access policy.
    """
    if not isinstance(provider, str) or not SAFE_SCOPE.fullmatch(provider):
        raise ConnectorError("invalid_adapter_provider", "provider must be a safe adapter selector")
    if not isinstance(scope, str) or not SAFE_SCOPE.fullmatch(scope):
        raise ConnectorError("invalid_adapter_scope", "scope must be a safe adapter selector")
    if not isinstance(protocol_version, str) or protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ConnectorError("invalid_adapter_protocol", "The selected adapter protocol is unsupported")
    config = {"max_assets": max_assets, "max_columns": max_columns,
              "retain_semantic_samples": retain_semantic_samples, "source_id": source_id, "name": name}
    if asset_names is not None:
        config["asset_names"] = asset_names
    asset_names, max_assets, max_columns, retain_samples = _scan_settings(config)
    payload = decode_response(raw)
    profile = {"provider": provider}
    if not isinstance(payload, dict) or payload.get("protocol_version") != protocol_version or not isinstance(payload.get("assets"), list):
        raise ConnectorError("adapter_response_invalid", "The adapter returned an incompatible metadata response")
    if (protocol_version in {"1.1", "1.2"} and payload.get("listing_complete") is not True
            or payload.get("listing_complete", True) is not True
            or payload.get("next_cursor") is not None):
        raise ConnectorError("adapter_listing_incomplete", "The adapter listing is incomplete; narrow the selected scope")
    raw_assets = payload["assets"]
    if len(raw_assets) > max_assets:
        raise ConnectorError("asset_limit_exceeded", "The adapter response exceeds max_assets")
    source_id = config.get("source_id") or stable_id("source", "external_adapter", profile["provider"], scope, config.get("name"))
    name = config.get("name") if isinstance(config.get("name"), str) else f"{profile['provider']} source"
    assets = []
    columns_seen = 0
    pointers: set[str] = set()
    upstream_ids: set[tuple[str, str, str]] = set()
    for item in raw_assets:
        if not isinstance(item, dict):
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid asset")
        asset_name = _metadata_string(item.get("name"), "asset name")
        pointer = item.get("pointer")
        if not _valid_pointer(pointer) or pointer in pointers:
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid asset pointer")
        pointers.add(pointer)
        asset_type = item.get("asset_type", "table")
        allowed_types = {"table", "document", "file"} | ({"semantic_definition"} if protocol_version == "1.2" else set())
        if not isinstance(asset_type, str) or asset_type not in allowed_types:
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid asset type")
        if "upstream_definition" in item and asset_type != "semantic_definition":
            raise ConnectorError("adapter_response_invalid", "Upstream definitions require a negotiated semantic definition asset")
        upstream = _upstream_definition(item.get("upstream_definition"), asset_name) if asset_type == "semantic_definition" else None
        if upstream is not None:
            identity = (upstream["kind"], upstream["namespace"], upstream["id"])
            if identity in upstream_ids:
                raise ConnectorError("adapter_response_invalid", "The adapter returned duplicate upstream definition identities")
            upstream_ids.add(identity)
        raw_columns = item.get("schema", [])
        if not isinstance(raw_columns, list):
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid asset schema")
        if upstream is not None and raw_columns:
            raise ConnectorError("adapter_response_invalid", "Upstream semantic definitions must not declare table fields")
        columns_seen += len(raw_columns)
        if columns_seen > max_columns:
            raise ConnectorError("column_limit_exceeded", "The adapter response exceeds max_columns")
        schema = []
        column_names: set[str] = set()
        field_ids: set[int] = set()
        for column in raw_columns:
            if not isinstance(column, dict):
                raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid column")
            if column.get("nullable") is not None and not isinstance(column["nullable"], bool):
                raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid column")
            column_name = _metadata_string(column.get("name"), "column name")
            if column_name in column_names:
                raise ConnectorError("adapter_response_invalid", "The adapter returned duplicate column names")
            column_names.add(column_name)
            native_type = _native_type(column.get("native_type", column.get("data_type", "unknown")))
            field = {"name": column_name, "data_type": type_name(native_type),
                     "native_type": native_type, "nullable": column.get("nullable")}
            if protocol_version in {"1.1", "1.2"} and "field_id" in column:
                field_id = column["field_id"]
                if type(field_id) is not int or not 1 <= field_id <= 2**31 - 1 or field_id in field_ids:
                    raise ConnectorError("adapter_response_invalid", "The adapter returned invalid field IDs")
                field_ids.add(field_id)
                field["field_id"] = field_id
            schema.append(field)
        observations = _observations(item, column_names) if protocol_version in {"1.1", "1.2"} else {}
        revision = item.get("revision")
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", revision)):
            raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid revision")
        full_pointer = f"adapter/{profile['provider']}/{scope}/{pointer}"
        document_signals = {"candidate_terms": [], "candidate_descriptions": [], "quantified_claims": []}
        document_profile: dict[str, Any] = {}
        if asset_type == "document":
            if protocol_version in {"1.1", "1.2"}:
                coverage = item.get("document_coverage")
                if retain_samples and coverage is None:
                    raise ConnectorError("adapter_response_invalid", "Document retention requires an explicit coverage declaration")
                if coverage is not None:
                    flags = {"extraction_complete", "extraction_truncated", "retention_complete", "retention_truncated"}
                    if (not isinstance(coverage, dict) or set(coverage) != flags
                            or any(type(coverage[key]) is not bool for key in flags)
                            or coverage["extraction_complete"] and coverage["extraction_truncated"]
                            or coverage["retention_complete"] and (coverage["retention_truncated"] or not coverage["extraction_complete"])):
                        raise ConnectorError("adapter_response_invalid", "The adapter returned contradictory document coverage")
                    document_profile = {
                        "document_extraction": {"complete": coverage["extraction_complete"], "truncated": coverage["extraction_truncated"]},
                        "document_retention": {"enabled": retain_samples,
                            "complete": coverage["retention_complete"] if retain_samples else None,
                            "truncated": coverage["retention_truncated"] if retain_samples else False},
                    }
            raw_terms = item.get("document_terms", [])
            if not isinstance(raw_terms, list) or len(raw_terms) > MAX_DOCUMENT_TERMS:
                raise ConnectorError("adapter_response_invalid", "The adapter returned too many document terms")
            for term_item in raw_terms:
                if not isinstance(term_item, dict):
                    raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document term")
                term = _metadata_string(term_item.get("term"), "document term", 96)
                document_signals["candidate_terms"].append(term)
                excerpt = term_item.get("excerpt")
                if excerpt is not None and retain_samples:
                    if not isinstance(excerpt, str) or not 4 <= len(excerpt) <= 280 or len(excerpt.strip()) < 4:
                        raise ConnectorError("adapter_response_invalid", "The adapter returned an invalid document excerpt")
                    description = {
                        "term": term, "excerpt": excerpt, "line": term_item.get("line")
                        if type(term_item.get("line")) is int and 0 < term_item["line"] <= 100000 else None,
                    }
                    if protocol_version in {"1.1", "1.2"} and "source_locator" in term_item:
                        description["source_locator"] = _document_locator(term_item["source_locator"])
                    document_signals["candidate_descriptions"].append(description)
        revision_facts = {key: value for key, value in observations.items() if key != "source_observed_at"}
        if upstream is not None:
            revision_facts["upstream_definition"] = {key: value for key, value in upstream.items() if key != "source_observed_at"}
        if asset_type == "document":
            revision_facts.update(document_signals=document_signals, document_coverage=document_profile)
        assets.append({
            "id": stable_id("asset", source_id, "upstream_definition", upstream["kind"], upstream["namespace"], upstream["id"])
            if upstream is not None else stable_id("asset", source_id, full_pointer),
            "name": asset_name,
            "asset_type": asset_type,
            "source_pointer": full_pointer,
            # An adapter revision is only a change hint. Bind the evidence identity
            # to the schema we observed even if a vendor forgets to bump it.
            "revision": stable_id("adapter_revision_schema", revision, schema,
                                  revision_facts),
            "revision_basis": "upstream_definition_fingerprint" if upstream is not None else "adapter_revision_and_schema" if revision else "schema_fingerprint",
            "schema": schema,
            "observations": observations,
            **({"upstream_definition": upstream} if upstream is not None else {}),
            "profile": {
                "metadata_only": True, "row_count": observations.get("row_count"),
                "row_count_basis": observations.get("row_count_basis", "unknown"),
                "row_count_is_complete": observations.get("row_count_basis") == "exact",
                "profile_rows": 0, "profile_is_sampled": False,
                "null_counts": {}, "distinct_counts": {},
                "document_signals": document_signals if asset_type == "document" else {},
                "content_retained": bool(document_signals["candidate_descriptions"]),
                **document_profile,
                **{key: observations[key] for key in ("primary_key", "foreign_keys") if key in observations},
            },
            "trust_state": "unverified",
        })
    assets.sort(key=lambda item: item["source_pointer"])
    by_pointer = {item["source_pointer"]: item for item in assets}
    for asset in assets:
        for key in asset.get("observations", {}).get("foreign_keys", []):
            target = by_pointer.get(f"adapter/{profile['provider']}/{scope}/{key['target_pointer']}")
            if target is not None and not set(key["target_columns"]) <= {field["name"] for field in target["schema"]}:
                raise ConnectorError("adapter_response_invalid", "The adapter returned a foreign key with unknown target fields")
    # Selection is a local projection of the fully validated, budgeted response.
    # It is never forwarded to the operator-owned endpoint as an access request.
    if asset_names is not None:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for asset in assets:
            by_name.setdefault(asset["name"], []).append(asset)
        if any(name not in by_name for name in asset_names):
            raise ConnectorError("adapter_asset_not_found", "A requested asset name is absent from the provisioned scope")
        if any(len(by_name[name]) != 1 for name in asset_names):
            raise ConnectorError("adapter_asset_name_ambiguous", "A requested asset name matches multiple provisioned assets")
        assets = [asset for asset in assets if asset["name"] in asset_names]
    revision_parts = [(item["source_pointer"], item["revision"]) for item in assets]
    revision = (stable_id("revision", {"asset_names": asset_names, "assets": revision_parts})
                if asset_names is not None else stable_id("revision", *revision_parts))
    return {
        "source": {
            "id": source_id, "name": name, "kind": "external_adapter",
            "protocol_version": protocol_version,
            "listing_complete_declared": payload.get("listing_complete") is True,
            "location": f"adapter/{profile['provider']}/{scope}",
            "revision": revision,
            **({"asset_selection": {"asset_names": asset_names, "complete": True,
                                    "completeness_scope": "requested_asset_names"}}
               if asset_names is not None else {}),
            "trust_state": "unverified",
        },
        "assets": assets,
        "capabilities": {
            "metadata": True, "safe_profiling": False, "documents": any(item["asset_type"] == "document" for item in assets),
            "change_signals": True, "query_execution": False, "permission_mirroring": False,
        },
        "errors": [],
    }


def discover(config: dict[str, Any]) -> dict[str, Any]:
    secret_ref = config.get("secret_ref")
    scope = config.get("scope")
    if not isinstance(secret_ref, str) or not SAFE_REFERENCE.fullmatch(secret_ref):
        raise ConnectorError("invalid_secret_reference", "A named server-side adapter reference is required")
    if not isinstance(scope, str) or not SAFE_SCOPE.fullmatch(scope):
        raise ConnectorError("invalid_adapter_scope", "scope must be a safe adapter selector")
    asset_names, max_assets, max_columns, retain_samples = _scan_settings(config)
    profile = _profile(secret_ref, scope)
    protocol_version = profile.get("protocol_version", PROTOCOL_VERSION)
    if protocol_version == "1.3":
        raise ConnectorError("adapter_inventory_mode_required", "Paged profiles require explicit inventory discovery")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if profile.get("bearer_token"):
        headers["Authorization"] = f"Bearer {profile['bearer_token']}"
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client,
            client.stream(
                "POST", profile["endpoint"], headers=headers,
                json={"protocol_version": protocol_version, "scope": scope, "max_assets": max_assets,
                      "max_columns": max_columns, "include_document_excerpts": retain_samples},
            ) as response,
        ):
            if response.status_code != 200:
                raise ConnectorError("adapter_unavailable", "The external source adapter did not return metadata")
            if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                raise ConnectorError("adapter_encoding_refused", "Compressed adapter metadata is not supported")
            chunks = []
            size = 0
            for chunk in response.iter_raw():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ConnectorError("adapter_response_too_large", "The adapter metadata response exceeds the size limit")
                chunks.append(chunk)
    except ConnectorError:
        raise
    except httpx.HTTPError as exc:
        raise ConnectorError("adapter_unavailable", "The external source adapter did not return valid metadata") from exc
    return normalize_response(
        b"".join(chunks), provider=profile["provider"], scope=scope,
        protocol_version=protocol_version, max_assets=max_assets, max_columns=max_columns,
        retain_semantic_samples=retain_samples, asset_names=asset_names,
        source_id=config.get("source_id"), name=config.get("name"),
    )
