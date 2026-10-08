"""Data-only JSON Pointer mappings from captured catalog records to exports.

No HTTP, scripts, expressions, credential discovery or vendor paging runs here.
The producer must supply truthful bounded extraction and coverage declarations.
"""

from __future__ import annotations

from copy import deepcopy

from knowledge_layer.adapters.catalog import (
    MAX_BYTES,
    MAX_RECORDS,
    SCHEMA_VERSION,
    inventory_records,
    scope_digest,
)
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import decode_response
from knowledge_layer.inventory import _encoded, inventory_digest
from knowledge_layer.store import KnowledgeValidationError

_KEYS = {"records_pointer", "id_pointer", "id_encoding", "type_pointer", "native_type", "name_pointer", "kind_by_type"}
_KINDS = {"asset", "semantic_definition", "relationship", "document"}


def _fail(code="catalog_mapping_invalid"):
    return ConnectorError(code, "The operator catalog mapping or captured records are unavailable")


def _tokens(pointer):
    if not isinstance(pointer, str) or len(pointer) > 512 or pointer and not pointer.startswith("/"):
        raise _fail()
    if not pointer:
        return []
    tokens = pointer[1:].split("/")
    if len(tokens) > 32:
        raise _fail()
    for token in tokens:
        for index, char in enumerate(token):
            if char == "~" and (index + 1 == len(token) or token[index + 1] not in "01"):
                raise _fail()
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def _value(value, pointer):
    for token in _tokens(pointer):
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isascii() and token.isdecimal() and (token == "0" or not token.startswith("0")):
            index = int(token)
            if index >= len(value):
                raise _fail("catalog_mapping_field_missing")
            value = value[index]
        else:
            raise _fail("catalog_mapping_field_missing")
    return value


def validate_mapping(mapping):
    if not isinstance(mapping, dict) or set(mapping) != _KEYS:
        raise _fail()
    for key in ("records_pointer", "id_pointer"):
        _tokens(mapping[key])
    for key in ("type_pointer", "name_pointer"):
        if mapping[key] is not None:
            _tokens(mapping[key])
    native_type = mapping["native_type"]
    if ((mapping["type_pointer"] is None) == (native_type is None)
            or native_type is not None and (not isinstance(native_type, str) or not 1 <= len(native_type) <= 120)
            or not isinstance(mapping["id_encoding"], str)
            or mapping["id_encoding"] not in {"string", "integer_decimal"}):
        raise _fail()
    kinds = mapping["kind_by_type"]
    if (not isinstance(kinds, dict) or not 1 <= len(kinds) <= 100
            or any(not isinstance(key, str) or not 1 <= len(key) <= 120
                   or not isinstance(value, str) or value not in _KINDS for key, value in kinds.items())):
        raise _fail()
    try:
        if len(_encoded(mapping)) > 16 * 1024:
            raise _fail()
    except (ValueError, UnicodeError, KnowledgeValidationError):
        raise _fail() from None
    return deepcopy(mapping)


def binding_digest(expected_scope, mapping=None):
    """Pin native source/population and mapping into the platform profile."""
    checked = validate_mapping(mapping) if mapping is not None else None
    return inventory_digest({"scope_digest": scope_digest(expected_scope), "mapping": checked})


def map_export(raw: bytes, *, contract: dict, mapping: dict) -> bytes:
    """Return a strict portable package, preserving each selected native object."""
    checked = validate_mapping(mapping)
    if (not isinstance(contract, dict) or set(contract) != {
            "scope", "observation", "listing_complete", "next_cursor", "record_count"}):
        raise _fail()
    try:
        document = decode_response(raw, max_bytes=MAX_BYTES)
    except ConnectorError:
        raise _fail("catalog_mapping_input_invalid") from None
    records = _value(document, checked["records_pointer"])
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise _fail()
    mapped = []
    for record in records:
        if not isinstance(record, dict):
            raise _fail()
        identifier = _value(record, checked["id_pointer"])
        if checked["id_encoding"] == "integer_decimal":
            if type(identifier) is not int:
                raise _fail()
            identifier = str(identifier)
        elif not isinstance(identifier, str):
            raise _fail()
        native_type = (_value(record, checked["type_pointer"]) if checked["type_pointer"] is not None
                       else checked["native_type"])
        if not isinstance(native_type, str) or native_type not in checked["kind_by_type"]:
            raise _fail("catalog_mapping_type_unsupported")
        name = _value(record, checked["name_pointer"]) if checked["name_pointer"] is not None else None
        mapped.append({"native_id": identifier, "native_type": native_type,
                       "kind": checked["kind_by_type"][native_type], "name": name, "native_record": record})
    package = {"schema_version": SCHEMA_VERSION, **deepcopy(contract), "records": mapped}
    try:
        encoded = _encoded(package)
    except (ValueError, UnicodeError, KnowledgeValidationError):
        raise _fail() from None
    inventory_records(encoded, expected_scope=contract["scope"])
    return encoded
