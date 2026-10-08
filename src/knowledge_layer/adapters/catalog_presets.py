"""Catalog translation presets for Alation, Collibra, Atlan, OpenMetadata and DataHub."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from knowledge_layer.adapters.catalog import MAX_BYTES, MAX_RECORDS, validate_contract
from knowledge_layer.adapters.catalog_mapping import _value, map_export, validate_mapping
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import decode_response

# ---------------------------------------------------------------------------
# Pre-configured JSON Pointer Mappings for Industry Catalogs
# ---------------------------------------------------------------------------

ALATION_TABLE_MAPPING: dict[str, Any] = {
    "records_pointer": "",
    "id_pointer": "/id",
    "id_encoding": "integer_decimal",
    "type_pointer": None,
    "native_type": "Table",
    "name_pointer": "/name",
    "kind_by_type": {
        "Table": "asset",
        "View": "asset",
    },
}

ALATION_WRAPPED_TABLE_MAPPING: dict[str, Any] = {
    "records_pointer": "/tables",
    "id_pointer": "/id",
    "id_encoding": "integer_decimal",
    "type_pointer": "/table_type",
    "native_type": None,
    "name_pointer": "/name",
    "kind_by_type": {
        "TABLE": "asset",
        "VIEW": "asset",
        "Table": "asset",
        "View": "asset",
    },
}

ALATION_TERM_MAPPING: dict[str, Any] = {
    "records_pointer": "",
    "id_pointer": "/id",
    "id_encoding": "integer_decimal",
    "type_pointer": None,
    "native_type": "GlossaryTerm",
    "name_pointer": "/title",
    "kind_by_type": {
        "GlossaryTerm": "semantic_definition",
    },
}

COLLIBRA_ASSET_MAPPING: dict[str, Any] = {
    "records_pointer": "/results",
    "id_pointer": "/id",
    "id_encoding": "string",
    "type_pointer": "/type/name",
    "native_type": None,
    "name_pointer": "/name",
    "kind_by_type": {
        "Table": "asset",
        "Database View": "asset",
        "Data Asset": "asset",
        "Business Term": "semantic_definition",
        "Metric": "semantic_definition",
        "Governance Asset": "document",
    },
}

COLLIBRA_DIRECT_ASSET_MAPPING: dict[str, Any] = {
    "records_pointer": "",
    "id_pointer": "/id",
    "id_encoding": "string",
    "type_pointer": "/type/name",
    "native_type": None,
    "name_pointer": "/name",
    "kind_by_type": {
        "Table": "asset",
        "Database View": "asset",
        "Data Asset": "asset",
        "Business Term": "semantic_definition",
        "Metric": "semantic_definition",
        "Governance Asset": "document",
    },
}

ATLAN_ENTITY_MAPPING: dict[str, Any] = {
    "records_pointer": "/entities",
    "id_pointer": "/guid",
    "id_encoding": "string",
    "type_pointer": "/typeName",
    "native_type": None,
    "name_pointer": "/attributes/name",
    "kind_by_type": {
        "Table": "asset",
        "View": "asset",
        "MaterialisedView": "asset",
        "Column": "asset",
        "GlossaryTerm": "semantic_definition",
        "AtlasGlossary": "document",
    },
}

OPENMETADATA_TABLE_MAPPING: dict[str, Any] = {
    "records_pointer": "/data",
    "id_pointer": "/id",
    "id_encoding": "string",
    "type_pointer": "/tableType",
    "native_type": None,
    "name_pointer": "/name",
    "kind_by_type": {
        "Regular": "asset",
        "View": "asset",
        "SecureView": "asset",
        "MaterializedView": "asset",
        "Table": "asset",
    },
}

OPENMETADATA_GLOSSARY_MAPPING: dict[str, Any] = {
    "records_pointer": "/data",
    "id_pointer": "/id",
    "id_encoding": "string",
    "type_pointer": None,
    "native_type": "GlossaryTerm",
    "name_pointer": "/name",
    "kind_by_type": {
        "GlossaryTerm": "semantic_definition",
    },
}

DATAHUB_ENTITY_MAPPING: dict[str, Any] = {
    "records_pointer": "/entities",
    "id_pointer": "/urn",
    "id_encoding": "string",
    "type_pointer": "/type",
    "native_type": None,
    "name_pointer": "/name",
    "kind_by_type": {
        "DATASET": "asset",
        "GLOSSARY_TERM": "semantic_definition",
        "CONTAINER": "asset",
        "DASHBOARD": "asset",
    },
}

PRESET_MAPPINGS: dict[str, dict[str, Any]] = {
    "alation": ALATION_TABLE_MAPPING,
    "alation_tables": ALATION_WRAPPED_TABLE_MAPPING,
    "alation_terms": ALATION_TERM_MAPPING,
    "collibra": COLLIBRA_ASSET_MAPPING,
    "collibra_direct": COLLIBRA_DIRECT_ASSET_MAPPING,
    "atlan": ATLAN_ENTITY_MAPPING,
    "openmetadata": OPENMETADATA_TABLE_MAPPING,
    "openmetadata_terms": OPENMETADATA_GLOSSARY_MAPPING,
    "datahub": DATAHUB_ENTITY_MAPPING,
}


def get_preset_mapping(format_name: str) -> dict[str, Any]:
    """Retrieve and validate a named catalog mapping preset."""
    normalized = format_name.lower().strip().replace("-", "_")
    if normalized not in PRESET_MAPPINGS:
        raise ConnectorError(
            "unknown_catalog_format",
            f"Unknown catalog format '{format_name}'. Available: {sorted(PRESET_MAPPINGS.keys())}",
        )
    return validate_mapping(PRESET_MAPPINGS[normalized])


def _translation_contract(
    record_count: int,
    *,
    scope: dict[str, Any] | None,
    observation: dict[str, Any] | None,
    listing_complete: bool | None,
    next_cursor: str | None,
) -> dict[str, Any]:
    """Preserve producer declarations; absent provenance remains unknown."""
    if listing_complete is not True or next_cursor is not None:
        raise ConnectorError(
            "catalog_export_incomplete",
            "Declare a complete captured listing with no continuation before translating it",
        )
    unknown_observation: dict[str, Any] = {
        "exported_at": None,
        "source_observed_at": None,
        "native_revision": None,
        "field_coverage": {"definitions": "unknown", "policies": "unknown"},
        "redaction": "unknown",
    }
    if observation is not None:
        if not isinstance(observation, dict):
            raise ConnectorError("catalog_export_invalid", "Observation must contain producer declarations")
        unknown_observation.update(deepcopy(observation))
    return validate_contract({
        "scope": deepcopy(scope),
        "observation": unknown_observation,
        "listing_complete": listing_complete,
        "next_cursor": next_cursor,
        "record_count": record_count,
    })


def _check_upstream_paging(document: Any, records_pointer: str, count: int) -> None:
    """Refuse known page metadata that contradicts a complete captured export.

    Inspect response envelopes and paging objects, never fields inside native
    records. A caller's completeness declaration cannot override a cursor,
    nonzero offset, or a population count differing from the captured listing.
    """
    envelopes: list[dict[str, Any]] = []
    if isinstance(document, dict):
        envelopes.append(document)
    current = document
    for token in records_pointer[1:].split("/")[:-1] if records_pointer else []:
        current = _value(current, "/" + token)
        if isinstance(current, dict):
            envelopes.append(current)
    paging_keys = {"paging", "pagination", "pageInfo", "page_info", "meta", "links"}
    cursor_keys = {
        "next_cursor", "nextCursor", "nextPageToken", "next_page_token", "next-page-token",
        "continuationToken", "continuation_token", "after", "scrollId", "scroll_id", "next",
        "nextLink", "next_link", "nextPage", "next_page",
    }
    more_keys = {"hasMore", "has_more", "hasNextPage", "has_next_page"}
    complete_keys = {"listing_complete", "isComplete", "is_complete", "isLast", "is_last"}
    offset_keys = {"offset", "start", "startIndex", "start_index"}
    total_keys = {"total", "totalCount", "total_count", "approximateCount"}

    def refuse() -> None:
        raise ConnectorError("catalog_export_incomplete", "Upstream paging contradicts a complete captured listing")

    # Only named metadata objects recurse, with a small fixed depth budget.
    pending = [(envelope, 0) for envelope in envelopes]
    while pending:
        envelope, depth = pending.pop()
        for key in cursor_keys & envelope.keys():
            if envelope[key] is not None and envelope[key] != "":
                refuse()
        for key in more_keys & envelope.keys():
            if envelope[key] is not False:
                refuse()
        for key in complete_keys & envelope.keys():
            if envelope[key] is not True:
                refuse()
        for key in offset_keys & envelope.keys():
            if type(envelope[key]) is not int or envelope[key] != 0:
                refuse()
        for key in total_keys & envelope.keys():
            if type(envelope[key]) is not int or envelope[key] != count:
                refuse()
        for key in paging_keys & envelope.keys():
            nested = envelope[key]
            if not isinstance(nested, dict) or depth >= 4:
                refuse()
            pending.append((nested, depth + 1))


def translate_catalog_export(
    raw_data: bytes | str | dict[str, Any] | list[Any],
    catalog_format: str,
    *,
    mapping: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    listing_complete: bool | None = None,
    next_cursor: str | None = None,
) -> bytes:
    """Translate a complete captured export into a portable catalog package.

    Supply the exact five-field scope and explicitly declare listing_complete=True
    only after capturing the whole selected population. Missing source clocks,
    native revision, field coverage and redaction are preserved as unknown. This
    offline translation neither verifies the producer's declarations nor stores
    the result in a registry. Partial or contradictory upstream paging refuses.
    """
    try:
        if isinstance(raw_data, (dict, list)):
            raw_bytes = json.dumps(raw_data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(raw_data, str):
            raw_bytes = raw_data.encode("utf-8")
        elif isinstance(raw_data, (bytes, bytearray)):
            raw_bytes = bytes(raw_data)
        else:
            raise ConnectorError("catalog_export_invalid", "raw_data must be bytes, string, dict, or list")
    except (TypeError, ValueError, UnicodeError):
        raise ConnectorError("catalog_export_invalid", "Export must contain finite UTF-8 JSON") from None

    if len(raw_bytes) > MAX_BYTES:
        raise ConnectorError("catalog_export_too_large", f"Export exceeds maximum size of {MAX_BYTES} bytes")

    resolved_mapping = validate_mapping(mapping) if mapping is not None else get_preset_mapping(catalog_format)

    # Determine record count from the raw payload
    try:
        parsed = decode_response(raw_bytes, max_bytes=MAX_BYTES)
    except ConnectorError:
        raise ConnectorError("catalog_export_invalid", "Export contains invalid JSON") from None

    ptr = resolved_mapping["records_pointer"]
    target_records = _value(parsed, ptr)

    if not isinstance(target_records, list):
        raise ConnectorError("catalog_export_invalid", f"Pointer '{ptr}' did not resolve to a list of records")

    count = len(target_records)
    if count > MAX_RECORDS:
        raise ConnectorError("catalog_record_limit_exceeded", f"Export contains {count} records, exceeding max {MAX_RECORDS}")

    contract = _translation_contract(
        record_count=count,
        scope=scope,
        observation=observation,
        listing_complete=listing_complete,
        next_cursor=next_cursor,
    )
    if mapping is None:
        preset = catalog_format.lower().strip().replace("-", "_")
        family = preset.split("_", 1)[0]
        provider = contract["scope"]["provider"].lower().replace("-", "_")
        family_presets = {name for name in PRESET_MAPPINGS if name.split("_", 1)[0] == family}
        if provider not in family_presets:
            raise ConnectorError(
                "catalog_scope_mismatch",
                "The declared provider does not belong to the selected catalog preset family",
            )
    _check_upstream_paging(parsed, ptr, count)

    return map_export(raw_bytes, contract=contract, mapping=resolved_mapping)
