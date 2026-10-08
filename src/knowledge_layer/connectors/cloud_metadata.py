"""Finite shared budgets and metadata projections for native cloud catalogs."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote

from knowledge_layer.connectors.common import ConnectorError, safe_identifier, stable_id
from knowledge_layer.connectors.secrets import read_source_secret

MAX_TABLES = 100
MAX_COLUMNS = 5_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DISCOVERY_SECONDS = 60
REQUEST_TIMEOUT = 10
CONNECT_TIMEOUT = 5


def scope_config(config: dict[str, Any], provider: str, pattern: str) -> tuple[str, str, list[str] | None, int, int]:
    if config.get("provider", provider) != provider:
        raise ConnectorError("database_provider_mismatch", "The selected provider does not match this native connector")
    secret_ref = config.get("secret_ref")
    if not isinstance(secret_ref, str) or not re.fullmatch(r"KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}", secret_ref):
        raise ConnectorError("invalid_secret_reference", "A named server-side source reference is required")
    scope = config.get("schema")
    if not isinstance(scope, str) or not re.fullmatch(pattern, scope) or len(scope) > 128:
        raise ConnectorError("cloud_scope_required", "Native cloud metadata requires an explicit qualified database/schema or project/dataset scope")
    tables = config.get("tables")
    if tables is not None and (
        not isinstance(tables, list) or not tables or len(tables) > MAX_TABLES
        or any(not safe_identifier(table) for table in tables) or len(set(tables)) != len(tables)
    ):
        raise ConnectorError("invalid_table_selector", "Select at most 100 distinct safe table names")
    max_tables, max_columns = config.get("max_tables", MAX_TABLES), config.get("max_columns", MAX_COLUMNS)
    if type(max_tables) is not int or not 1 <= max_tables <= MAX_TABLES:
        raise ConnectorError("invalid_scan_limit", "max_tables must be between 1 and 100")
    if type(max_columns) is not int or not 1 <= max_columns <= MAX_COLUMNS:
        raise ConnectorError("invalid_scan_limit", "max_columns must be between 1 and 5000")
    if tables is not None and len(tables) > max_tables:
        raise ConnectorError("table_limit_exceeded", "The selected cloud scope exceeds max_tables")
    return secret_ref, scope, sorted(tables) if tables is not None else None, max_tables, max_columns


def secret_object(reference: str, provider: str) -> dict[str, Any]:
    raw = read_source_secret(reference, unavailable_code="database_credentials_unavailable")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ConnectorError("database_secret_invalid", "The native cloud source secret must be a JSON credential profile") from exc
    if not isinstance(value, dict):
        raise ConnectorError("database_secret_invalid", "The native cloud source secret must be a JSON credential profile")
    return value


class MetadataBudget:
    def __init__(self):
        self.deadline = time.monotonic() + MAX_DISCOVERY_SECONDS
        self.bytes = 0

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError("cloud_discovery_timeout", "Cloud metadata discovery exceeded its time budget; narrow the selected scope")
        return min(REQUEST_TIMEOUT, remaining)

    def consume_bytes(self, size: int) -> None:
        self.timeout()
        self.bytes += size
        if self.bytes > MAX_RESPONSE_BYTES:
            raise ConnectorError("cloud_metadata_too_large", "Cloud metadata exceeds the response budget; narrow the selected scope")

    def consume_json(self, value: Any) -> None:
        try:
            for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
                self.consume_bytes(len(chunk.encode("utf-8")))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ConnectorError("cloud_metadata_invalid", "The cloud catalog returned invalid metadata") from exc


def text(value: Any, *, maximum: int = 16_384, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or (not optional and not value) or len(value.encode("utf-8")) > maximum:
        raise ConnectorError("cloud_metadata_invalid", "The cloud catalog returned invalid or excessive metadata text")
    return value


def asset(
    source_id: str, provider: str, identity: list[str], name: str,
    schema: list[dict[str, Any]], observations: dict[str, Any],
) -> dict[str, Any]:
    pointer = "/".join(["sql", provider, *(quote(part, safe="") for part in identity), quote(name, safe="")])
    return {
        "id": stable_id("asset", source_id, pointer), "name": name, "asset_type": "table",
        "source_pointer": pointer,
        "revision": stable_id("schema", schema, observations), "revision_basis": "native_metadata_fingerprint",
        "schema": schema, "observations": observations,
        "profile": {
            "row_count": None, "row_count_is_complete": False, "profile_rows": 0,
            "profile_is_sampled": False, "null_counts": {}, "distinct_counts": {},
            "metadata_only": True, "content_retained": False,
            "primary_key_state": "unknown", "foreign_key_state": "unknown",
        },
        "trust_state": "unverified",
    }


def discovery(config: dict[str, Any], provider: str, source_id: str, identity: list[str], assets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source": {
            "id": source_id, "name": config.get("name") or f"{provider} metadata source", "kind": "sql_catalog",
            "location": "/".join(["sql", provider, *(quote(part, safe="") for part in identity)]),
            "revision": stable_id("revision", *[(row["source_pointer"], row["revision"]) for row in assets]),
            "trust_state": "unverified",
        },
        "assets": assets,
        "capabilities": {
            "metadata": True, "safe_profiling": False, "documents": False, "change_signals": True,
            "query_execution": False, "permission_mirroring": False,
        },
        "coverage": {"scope": config["schema"], "complete_selected_scope": True,
                     "visibility_basis": "metadata_visible_to_source_credential", "application_rows_read": False},
        "errors": [],
    }
