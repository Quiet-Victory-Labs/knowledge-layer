"""Tenant-scoped connection and metadata snapshot registry."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    delete,
    exists,
    false,
    func,
    insert,
    inspect,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.pool import StaticPool

from knowledge_layer.access import Principal, current_principal
from knowledge_layer.access_log import record_source_read
from knowledge_layer.connectors.common import normalize_name
from knowledge_layer.graph import project_graph, slice_legacy_graph
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.source_policy import SourcePolicy, source_identity

CONNECTOR_TYPES: dict[str, dict[str, Any]] = {
    "local_files": {
        "id": "local_files",
        "name": "Local files",
        "description": "Profile bounded metadata from an explicitly selected directory beneath the server-configured source root.",
        "config_fields": [
            {"name": "path", "type": "relative_path", "required": False, "default": ".", "options": []},
            {"name": "include", "type": "glob", "required": False, "default": "**/*"},
            {"name": "max_files", "type": "integer", "required": False, "default": 100, "minimum": 1, "maximum": 1000},
            {"name": "max_file_bytes", "type": "integer", "required": False, "default": 16777216, "minimum": 1024, "maximum": 16777216},
            {"name": "max_total_bytes", "type": "integer", "required": False, "default": 67108864, "minimum": 1048576, "maximum": 536870912},
            {"name": "profile_rows", "type": "integer", "required": False, "default": 5000, "minimum": 1, "maximum": 10000},
            {"name": "max_columns", "type": "integer", "required": False, "default": 256, "minimum": 1, "maximum": 512},
            {"name": "retain_semantic_samples", "type": "boolean", "required": False, "default": False, "description": "Retain bounded source entity labels, explicit edge identifiers, and short document excerpts for review. Enable only when source content may be stored in the registry."},
        ],
        "capabilities": {"metadata": True, "safe_profiling": True, "documents": True, "change_signals": True, "query_execution": False, "permission_mirroring": False},
    },
    "iceberg": {
        "id": "iceberg",
        "name": "Iceberg catalog",
        "description": "Read REST catalog metadata using an operator-provisioned catalog alias.",
        "config_fields": [
            {"name": "catalog", "type": "catalog_name", "required": True},
            {"name": "namespace", "type": "identifier", "required": True},
            {"name": "tables", "type": "identifier_list", "required": False},
        ],
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": False, "change_signals": True, "query_execution": False, "permission_mirroring": False},
    },
    "postgres": {
        "id": "postgres",
        "name": "PostgreSQL catalog",
        "description": "Read information_schema metadata using a server-side named secret reference.",
        "config_fields": [
            {"name": "secret_ref", "type": "environment_secret_reference", "required": True, "placeholder": "KNOWLEDGE_SOURCE_ORDERS", "description": "Name of a server-provisioned secret reference; do not enter a DSN or credential."},
            {"name": "schema", "type": "identifier", "required": False, "default": "public"},
            {"name": "tables", "type": "identifier_list", "required": False},
            {"name": "connect_timeout", "type": "integer", "required": False, "default": 5, "minimum": 1, "maximum": 10},
        ],
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": False, "change_signals": False, "query_execution": False, "permission_mirroring": False},
    },
    "sql_catalog": {
        "id": "sql_catalog",
        "name": "SQL database or warehouse",
        "description": "Read PostgreSQL, SQLite, ClickHouse, Snowflake or BigQuery metadata through read-only driver policies. Customer account qualification remains required.",
        "maturity": "adapter_preview",
        "config_fields": [
            {"name": "provider", "type": "choice", "required": True, "default": "postgresql", "options": [
                "postgresql", "clickhouse", "sqlite", "snowflake", "bigquery",
            ]},
            {"name": "secret_ref", "type": "environment_secret_reference", "required": True, "placeholder": "KNOWLEDGE_SOURCE_WAREHOUSE", "description": "Name of a server-provisioned URL or native Snowflake/BigQuery JSON profile; credentials stay on the server."},
            {"name": "schema", "type": "identifier", "required": False, "description": "Snowflake: DATABASE.SCHEMA; BigQuery: project.dataset; other providers: schema name."},
            {"name": "tables", "type": "identifier_list", "required": False},
            {"name": "max_tables", "type": "integer", "required": False, "default": 100, "minimum": 1, "maximum": 100},
            {"name": "max_columns", "type": "integer", "required": False, "default": 5000, "minimum": 1, "maximum": 5000},
        ],
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": False, "change_signals": True, "query_execution": False, "permission_mirroring": False},
    },
    "object_store": {
        "id": "object_store",
        "name": "S3-compatible files",
        "description": "List scoped object metadata from an operator-provisioned S3 bucket profile. Bounded document passages are read only when that server profile enables extraction.",
        "maturity": "adapter_preview",
        "config_fields": [
            {"name": "secret_ref", "type": "environment_secret_reference", "required": True, "placeholder": "KNOWLEDGE_SOURCE_FILES", "description": "Name of a server-provisioned bucket profile; do not enter storage keys."},
            {"name": "prefix", "type": "relative_path", "required": False, "default": ""},
            {"name": "max_objects", "type": "integer", "required": False, "default": 100, "minimum": 1, "maximum": 500},
        ],
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": True, "change_signals": True, "query_execution": False, "permission_mirroring": False},
    },
    "google_drive": {
        "id": "google_drive",
        "name": "Google Drive folder",
        "description": "Discover direct children of an operator-enabled folder. Bounded document passages are retained only when the server profile enables content extraction.",
        "maturity": "adapter_preview",
        "config_fields": [
            {"name": "secret_ref", "type": "environment_secret_reference", "required": True, "placeholder": "KNOWLEDGE_SOURCE_DRIVE", "description": "Name of a server-provisioned service-account and folder profile; credentials stay on the server."},
            {"name": "folder_id", "type": "identifier", "required": True, "description": "Exact Drive folder ID approved by the operator; subfolders need separate connections."},
            {"name": "max_files", "type": "integer", "required": False, "default": 100, "minimum": 1, "maximum": 100},
        ],
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": True, "change_signals": True, "query_execution": False, "permission_mirroring": False},
    },
    "external_adapter": {
        "id": "external_adapter",
        "name": "Enterprise source adapter",
        "description": "Connect a separately deployed source adapter for SaaS, APIs, or custom systems. An administrator provisions its endpoint, access, and allowed scopes.",
        "maturity": "adapter_protocol_v1",
        "config_fields": [
            {"name": "secret_ref", "type": "environment_secret_reference", "required": True, "placeholder": "KNOWLEDGE_SOURCE_CRM", "description": "Name of a server-provisioned adapter profile; do not enter its URL or token."},
            {"name": "scope", "type": "identifier", "required": True, "placeholder": "accounts", "description": "A scope enabled by the administrator for this adapter."},
            {"name": "discovery_mode", "type": "identifier", "required": True, "default": "snapshot",
             "options": [{"label": "Bounded semantic snapshot", "value": "snapshot"},
                         {"label": "Paged discovery inventory", "value": "inventory"}]},
            {"name": "retain_inventory_payloads", "type": "boolean", "default": False,
             "discovery_modes": ["inventory"], "label": "Retain complete inventory payloads"},
            {"name": "asset_names", "type": "identifier_list", "required": False,
             "discovery_modes": ["snapshot"],
             "description": "Optional exact asset names within the provisioned scope; every selected name must resolve uniquely."},
            {"name": "max_assets", "type": "integer", "required": False, "default": 100, "minimum": 1, "maximum": 100, "discovery_modes": ["snapshot"]},
            {"name": "max_columns", "type": "integer", "required": False, "default": 5000, "minimum": 1, "maximum": 5000, "discovery_modes": ["snapshot"]},
            {"name": "retain_semantic_samples", "type": "boolean", "required": False, "default": False,
             "discovery_modes": ["snapshot"],
             "description": "Allow bounded document term excerpts from the adapter to be retained as evidence."},
        ],
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": True, "change_signals": True, "query_execution": False, "permission_mirroring": False},
    },
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_CATALOG = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
_ENV_REFERENCE = re.compile(r"^KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}$")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CONNECTION_CURSOR_ID = re.compile(r"conn_[A-Za-z0-9_-]{1,155}\Z")
_CONNECTION_CURSOR_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z\Z")
_CONNECTION_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_SENSITIVE_KEY_PARTS = ("password", "token", "credential", "authorization", "api_key", "access_key", "private_key", "connection_string", "dsn")
_ALLOWED_SNAPSHOT_FIELDS = {
    "snapshot_id", "kind", "status", "source", "assets", "concepts", "relationships", "proposals", "metrics", "evidence", "drift", "errors", "capabilities", "discovery_complete", "meaning_certified", "agent_ready", "curation_status", "profiling",
}
_ALLOWED_SNAPSHOT_FIELDS.update({"curation_complete", "curation_errors"})
_VOLATILE_PROPOSAL_FIELDS = frozenset({
    "status", "certified", "decision", "decision_rationale", "decided_at",
    "effective_on_current_snapshot", "created_at", "updated_at",
})


class KnowledgeError(Exception):
    """Base error for the knowledge registry."""


class KnowledgeNotFound(KnowledgeError):
    """Raised when an identifier is missing from the requested tenant."""


class KnowledgeValidationError(KnowledgeError, ValueError):
    """Raised for invalid connector or proposal data."""


class KnowledgeConflict(KnowledgeError):
    """Raised when a decision conflicts with a recorded decision."""


class KnowledgeAccessDenied(KnowledgeError):
    """Raised when a principal lacks the required source-management role."""


class CurationFailed(KnowledgeError):
    """Raised when a source adapter fails to produce a snapshot."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _proposal_digest(proposal: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    """Bind a decision to proposal content and the evidence it cited."""
    content = {key: value for key, value in proposal.items() if key not in _VOLATILE_PROPOSAL_FIELDS}
    indexed = {_record_id(item): item for item in evidence if isinstance(item, dict)}
    content["cited_evidence"] = {
        key: indexed.get(key) for key in sorted(set(_evidence_ids(proposal)))
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_tenant_id(tenant_id: str | None) -> str:
    value = (tenant_id or "demo").strip()
    if not _TENANT.fullmatch(value):
        raise KnowledgeValidationError("tenant_id must be 1-64 safe identifier characters")
    return value


def _encode_connection_cursor(tenant_id: str, row: dict[str, Any]) -> str:
    payload = json.dumps([1, tenant_id, row["created_at"], row["id"]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_connection_cursor(tenant_id: str, cursor: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise KnowledgeValidationError("Connection cursor is invalid")
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, UnicodeError) as exc:
        raise KnowledgeValidationError("Connection cursor is invalid") from exc
    if (not isinstance(payload, list) or len(payload) != 4 or payload[0] != 1 or payload[1] != tenant_id
            or not isinstance(payload[2], str) or not _CONNECTION_CURSOR_AT.fullmatch(payload[2])
            or not isinstance(payload[3], str) or not _CONNECTION_CURSOR_ID.fullmatch(payload[3])):
        raise KnowledgeValidationError("Connection cursor is invalid")
    return payload[2], payload[3]


def validate_connection_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize selectors without accepting credential values."""
    if kind not in CONNECTOR_TYPES:
        raise KnowledgeValidationError(f"Unsupported connector kind: {kind}")
    if kind in {"iceberg", "object_store"}:
        from knowledge_layer.curation import CONNECTORS

        if kind not in CONNECTORS:
            raise KnowledgeValidationError(f"{kind} connector dependency is not installed")
    if not isinstance(config, dict):
        raise KnowledgeValidationError("config must be an object")

    allowlists = {
        "local_files": {"path", "include", "max_files", "max_file_bytes", "max_total_bytes", "profile_rows", "max_columns", "name", "retain_semantic_samples"},
        "iceberg": {"catalog", "namespace", "tables"},
        "postgres": {"secret_ref", "schema", "tables", "connect_timeout"},
        "sql_catalog": {"provider", "secret_ref", "schema", "tables", "max_tables", "max_columns"},
        "object_store": {"secret_ref", "prefix", "max_objects"},
        "google_drive": {"secret_ref", "folder_id", "max_files"},
        "external_adapter": {"secret_ref", "scope", "asset_names", "max_assets", "max_columns", "retain_semantic_samples",
                             "discovery_mode", "retain_inventory_payloads", "inventory_limits"},
    }
    unknown = set(config) - allowlists[kind]
    if unknown:
        raise KnowledgeValidationError("config contains unsupported or unsafe fields")

    if kind == "local_files":
        result: dict[str, Any] = {
            "path": _relative_path(config.get("path", "."), "path"),
            "include": _safe_glob(config.get("include", "**/*")),
            "max_files": _bounded_int(config.get("max_files", 100), "max_files", 1, 1000),
            "max_file_bytes": _bounded_int(config.get("max_file_bytes", 16 * 1024 * 1024), "max_file_bytes", 1024, 128 * 1024 * 1024),
            "max_total_bytes": _bounded_int(config.get("max_total_bytes", 64 * 1024 * 1024), "max_total_bytes", 1024 * 1024, 512 * 1024 * 1024),
            "profile_rows": _bounded_int(config.get("profile_rows", 5000), "profile_rows", 1, 10000),
            "max_columns": _bounded_int(config.get("max_columns", 256), "max_columns", 1, 512),
        }
        retain_samples = config.get("retain_semantic_samples", False)
        if not isinstance(retain_samples, bool):
            raise KnowledgeValidationError("retain_semantic_samples must be a boolean")
        if "retain_semantic_samples" in config:
            result["retain_semantic_samples"] = retain_samples
        display_name = config.get("name")
        if display_name is not None:
            result["name"] = _validate_name(display_name)
        return result

    if kind == "iceberg":
        catalog = config.get("catalog")
        namespace = config.get("namespace")
        if not isinstance(catalog, str) or not _CATALOG.fullmatch(catalog):
            raise KnowledgeValidationError("iceberg catalog must be a safe catalog name")
        if not isinstance(namespace, str) or not _IDENTIFIER.fullmatch(namespace):
            raise KnowledgeValidationError("iceberg namespace must be a safe identifier")
        result = {"catalog": catalog, "namespace": namespace}
        if "tables" in config:
            result["tables"] = _identifier_list(config["tables"], "tables")
        return result

    secret_ref = config.get("secret_ref")
    if not isinstance(secret_ref, str) or not _ENV_REFERENCE.fullmatch(secret_ref):
        raise KnowledgeValidationError(f"{kind} secret_ref must name a KNOWLEDGE_SOURCE_* environment variable")
    if kind == "google_drive":
        from knowledge_layer.connectors.google_drive import FILE_ID

        folder = config.get("folder_id")
        if not isinstance(folder, str) or not FILE_ID.fullmatch(folder):
            raise KnowledgeValidationError("folder_id must be an explicit safe Drive folder ID")
        return {"secret_ref": secret_ref, "folder_id": folder,
                "max_files": _bounded_int(config.get("max_files", 100), "max_files", 1, 100)}
    if kind == "object_store":
        prefix = config.get("prefix", "")
        if not isinstance(prefix, str) or prefix.startswith("/") or ".." in Path(prefix).parts or "\\" in prefix or len(prefix) > 256:
            raise KnowledgeValidationError("prefix must be a bounded relative object key prefix")
        return {
            "secret_ref": secret_ref,
            "prefix": prefix,
            "max_objects": _bounded_int(config.get("max_objects", 100), "max_objects", 1, 500),
        }
    if kind == "external_adapter":
        from knowledge_layer.connectors.common import ConnectorError
        from knowledge_layer.connectors.external_adapter import SAFE_SCOPE, validate_asset_names

        scope = config.get("scope")
        if not isinstance(scope, str) or not SAFE_SCOPE.fullmatch(scope):
            raise KnowledgeValidationError("scope must be a safe adapter selector")
        mode = config.get("discovery_mode", "snapshot")
        if not isinstance(mode, str) or mode not in {"snapshot", "inventory"}:
            raise KnowledgeValidationError("Adapter discovery mode is unsupported")
        if mode == "inventory":
            from knowledge_layer.inventory import _limits
            from knowledge_layer.inventory_refresh import DEFAULT_INVENTORY_LIMITS

            if set(config) & {"asset_names", "max_assets", "max_columns", "retain_semantic_samples"}:
                raise KnowledgeValidationError("Inventory mode cannot use legacy projection controls")
            if config.get("retain_inventory_payloads") is not True:
                raise KnowledgeValidationError("Inventory mode requires explicit complete-payload retention")
            return {"secret_ref": secret_ref, "scope": scope, "discovery_mode": "inventory",
                    "retain_inventory_payloads": True,
                    "inventory_limits": _limits(config.get("inventory_limits", DEFAULT_INVENTORY_LIMITS))}
        if set(config) & {"inventory_limits", "retain_inventory_payloads"}:
            raise KnowledgeValidationError("Inventory controls require inventory mode")
        retain_samples = config.get("retain_semantic_samples", False)
        if not isinstance(retain_samples, bool):
            raise KnowledgeValidationError("retain_semantic_samples must be a boolean")
        result = {
            "secret_ref": secret_ref,
            "scope": scope,
            "max_assets": _bounded_int(config.get("max_assets", 100), "max_assets", 1, 100),
            "max_columns": _bounded_int(config.get("max_columns", 5000), "max_columns", 1, 5000),
            "retain_semantic_samples": retain_samples,
        }
        if "asset_names" in config:
            try:
                result["asset_names"] = validate_asset_names(config["asset_names"])
            except ConnectorError as exc:
                raise KnowledgeValidationError(exc.public_message) from exc
        return result
    if kind == "sql_catalog":
        from knowledge_layer.connectors.sql_catalog import PROVIDERS

        provider = config.get("provider")
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise KnowledgeValidationError("provider must name an available SQL adapter family")
        result = {
            "provider": provider,
            "secret_ref": secret_ref,
            "max_tables": _bounded_int(config.get("max_tables", 100), "max_tables", 1, 100),
            "max_columns": _bounded_int(config.get("max_columns", 5000), "max_columns", 1, 5000),
        }
        if "schema" in config and config["schema"] not in (None, ""):
            pattern = (r"[a-z][a-z0-9-]{4,61}[a-z0-9]\.[A-Za-z_][A-Za-z0-9_]*"
                       if provider == "bigquery" else
                       r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"
                       if provider == "snowflake" else _IDENTIFIER.pattern)
            if (not isinstance(config["schema"], str) or len(config["schema"]) > 128
                    or not re.fullmatch(pattern, config["schema"])):
                raise KnowledgeValidationError("schema must be a safe provider-qualified scope")
            result["schema"] = config["schema"]
        if "tables" in config:
            result["tables"] = _identifier_list(config["tables"], "tables")
        return result
    schema = config.get("schema", "public")
    if not isinstance(schema, str) or not _IDENTIFIER.fullmatch(schema):
        raise KnowledgeValidationError("postgres schema must be a safe identifier")
    result = {
        "secret_ref": secret_ref,
        "schema": schema,
        "connect_timeout": _bounded_int(config.get("connect_timeout", 5), "connect_timeout", 1, 10),
    }
    if "tables" in config:
        result["tables"] = _identifier_list(config["tables"], "tables")
    return result


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not _CONNECTION_NAME.fullmatch(value.strip()):
        raise KnowledgeValidationError("name must be 1-120 printable characters")
    return value.strip()


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise KnowledgeValidationError(f"{field} must be an integer between {minimum} and {maximum}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise KnowledgeValidationError(f"{field} must be an integer between {minimum} and {maximum}") from exc
    if parsed < minimum or parsed > maximum:
        raise KnowledgeValidationError(f"{field} must be an integer between {minimum} and {maximum}")
    return parsed


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeValidationError(f"{field} must be a relative fixture path")
    path = Path(value.strip())
    if path.is_absolute() or ".." in path.parts or value.startswith("~"):
        raise KnowledgeValidationError(f"{field} must stay beneath the configured fixture root")
    normalized = path.as_posix()
    return "." if normalized in {"", "."} else normalized


def _safe_glob(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute() or ".." in Path(value).parts:
        raise KnowledgeValidationError("include must be a safe relative glob")
    return value.strip()


def _identifier_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise KnowledgeValidationError(f"{field} must be a list of at most 100 identifiers")
    if any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in value):
        raise KnowledgeValidationError(f"{field} may contain only safe identifiers")
    return list(dict.fromkeys(value))


def _sanitize(value: Any, key: str | None = None) -> Any:
    """Remove credential-like fields and redact absolute source paths."""
    if isinstance(value, dict):
        clean = {}
        for raw_key, raw_value in value.items():
            field = str(raw_key)
            folded = field.casefold()
            if field != "secret_ref" and any(part in folded for part in _SENSITIVE_KEY_PARTS):
                continue
            if field == "secret_ref":
                if isinstance(raw_value, str) and _ENV_REFERENCE.fullmatch(raw_value):
                    clean[field] = raw_value
                continue
            clean[field] = _sanitize(raw_value, field)
        return clean
    if isinstance(value, list):
        return [_sanitize(item, key) for item in value[:10000]]
    if isinstance(value, tuple):
        return [_sanitize(item, key) for item in value[:10000]]
    if isinstance(value, Path):
        value = value.as_posix()
    if isinstance(value, str):
        if key in {"source_pointer", "location", "path", "root"} and Path(value).is_absolute():
            return "[redacted absolute path]"
        return value[:20000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:20000]


MAX_SOURCE_PROPOSALS = 2_000
MAX_SUBMITTER_PROPOSALS = 250
STEWARD_PROPOSAL_RESERVE = 200


class KnowledgeStore:
    """SQLAlchemy-backed registry; reads and writes are always tenant-scoped."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        local_files_root: str | Path | None = None,
        initialize_schema: bool = True,
        access_enforced: bool = False,
    ) -> None:
        from knowledge_layer.context_receipts import ReceiptKeyProvider

        # Fail before creating a registry or opening a database on bad config.
        self._context_receipt_keys = ReceiptKeyProvider()
        configured_url = database_url or operator_secret("DATABASE_URL")
        if configured_url:
            url = _normalize_database_url(configured_url)
        else:
            db_path = Path(os.getenv("KNOWLEDGE_DB_PATH", ".local/knowledge-registry.sqlite3"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{db_path.resolve()}"
        is_sqlite_memory = url.startswith("sqlite") and ":memory:" in url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        if is_sqlite_memory:
            # StaticPool shares one sqlite3 connection between request threads.
            # CPython's statement cache can corrupt concurrent SELECT results
            # on that shared connection (python/cpython#118172).
            connect_args["cached_statements"] = 0
        self.engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
            poolclass=StaticPool if is_sqlite_memory else None,
        )
        self.backend = "postgresql" if url.startswith("postgresql") else "sqlite"
        self.access_enforced = access_enforced
        self.source_admin_group: str | None = None
        self.local_agent_subject: str | None = None
        self.source_policy: SourcePolicy | None = None
        self.source_policy_loader = None
        self._source_permissions_required = False
        self.local_files_root = Path(
            local_files_root or os.getenv("KNOWLEDGE_FIXTURE_ROOT") or "data/connectable"
        ).resolve()
        self.metadata = MetaData()
        self.connections = Table(
            "knowledge_connections",
            self.metadata,
            Column("id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False, index=True),
            Column("kind", String(32), nullable=False),
            Column("name", String(120), nullable=False),
            Column("config", JSON, nullable=False),
            Column("status", String(32), nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("updated_at", String(40), nullable=False),
            Column("last_refreshed_at", String(40)),
            Column("last_refresh_attempt_at", String(40)),
            Column("last_complete_refresh_at", String(40)),
            Column("last_error", Text),
            Index("ix_knowledge_connections_tenant_created_id", "tenant_id", "created_at", "id"),
        )
        self.snapshots = Table(
            "knowledge_snapshots",
            self.metadata,
            Column("row_id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False, index=True),
            Column("connection_id", String(64), nullable=False, index=True),
            Column("version", Integer, nullable=False),
            Column("snapshot_id", String(128), nullable=False),
            Column("status", String(32), nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("payload", JSON, nullable=False),
            UniqueConstraint("tenant_id", "connection_id", "version", name="uq_knowledge_snapshot_version"),
        )
        self.concept_search = Table(
            "knowledge_concept_search", self.metadata,
            Column("row_id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            Column("connection_id", String(64), nullable=False),
            Column("snapshot_version", Integer, nullable=False),
            Column("concept_id", String(160), nullable=False),
            Column("name", String(240), nullable=False),
            Column("definition", String(320)),
            Column("semantic_type", String(80)),
            Column("trust_state", String(40)),
            Column("evidence_ids", JSON, nullable=False),
            Column("evidence", JSON, nullable=False),
            Column("search_text", Text, nullable=False),
            UniqueConstraint("tenant_id", "connection_id", "concept_id",
                             name="uq_knowledge_concept_search_current"),
            Index("ix_knowledge_concept_search_scope", "tenant_id", "connection_id"),
        )
        Index("ix_knowledge_concept_search_text_gin",
              func.to_tsvector(text("'simple'::regconfig"), self.concept_search.c.search_text),
              postgresql_using="gin").ddl_if(dialect="postgresql")
        self.nodes = Table(
            "knowledge_graph_nodes",
            self.metadata,
            Column("row_id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            Column("connection_id", String(64), nullable=False),
            Column("version", Integer, nullable=False),
            Column("node_id", String(160), nullable=False),
            Column("kind", String(24), nullable=False),
            Column("label", String(240), nullable=False),
            Column("description", Text),
            Column("status", String(40), nullable=False),
            Column("trust_state", String(40), nullable=False),
            Column("evidence_ids", JSON, nullable=False),
            UniqueConstraint("tenant_id", "connection_id", "version", "node_id", name="uq_knowledge_graph_node"),
            Index("ix_knowledge_graph_nodes_scope", "tenant_id", "connection_id", "version", "kind"),
        )
        self.edges = Table(
            "knowledge_graph_edges",
            self.metadata,
            Column("row_id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            Column("connection_id", String(64), nullable=False),
            Column("version", Integer, nullable=False),
            Column("edge_id", String(240), nullable=False),
            Column("subject_id", String(160), nullable=False),
            Column("predicate", String(80), nullable=False),
            Column("object_id", String(160), nullable=False),
            Column("status", String(40), nullable=False),
            Column("trust_state", String(40), nullable=False),
            Column("evidence_ids", JSON, nullable=False),
            Column("confidence", Float),
            UniqueConstraint("tenant_id", "connection_id", "version", "edge_id", name="uq_knowledge_graph_edge"),
            Index("ix_knowledge_graph_edges_subject", "tenant_id", "connection_id", "version", "subject_id"),
            Index("ix_knowledge_graph_edges_object", "tenant_id", "connection_id", "version", "object_id"),
        )
        self.decisions = Table(
            "knowledge_proposal_decisions",
            self.metadata,
            Column("tenant_id", String(64), primary_key=True),
            Column("connection_id", String(64), primary_key=True),
            Column("proposal_id", String(128), primary_key=True),
            Column("decision", String(16), nullable=False),
            Column("rationale", Text),
            Column("decided_at", String(40), nullable=False),
            Column("proposal_digest", String(64)),
        )
        self.proposals = Table(
            "knowledge_semantic_proposals",
            self.metadata,
            Column("id", String(128), primary_key=True),
            Column("tenant_id", String(64), nullable=False, index=True),
            Column("connection_id", String(64), nullable=False, index=True),
            Column("proposal_type", String(64), nullable=False),
            Column("target_concept_id", String(160), nullable=False),
            Column("proposed_definition", Text),
            Column("proposed_mapping", JSON),
            Column("evidence_ids", JSON, nullable=False),
            Column("rationale", Text, nullable=False),
            Column("status", String(16), nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("submitted_by", String(255), nullable=False, server_default="legacy"),
            Column("archived_at", String(40)),
            Column("archived_by", String(255)),
            Column("archive_reason", String(1000)),
            Index("ix_knowledge_semantic_proposals_active_submitter", "tenant_id", "connection_id", "submitted_by", "archived_at"),
        )
        self.contract_proposals = Table(
            "knowledge_contract_proposals",
            self.metadata,
            Column("id", String(128), primary_key=True),
            Column("tenant_id", String(64), nullable=False, index=True),
            Column("connection_id", String(64), nullable=False, index=True),
            Column("source_snapshot_id", String(128), nullable=False),
            Column("assertion_kind", String(40), nullable=False),
            Column("asset_id", String(160), nullable=False),
            Column("payload", JSON, nullable=False),
            Column("evidence_ids", JSON, nullable=False),
            Column("evidence_refs", JSON, nullable=False),
            Column("rationale", Text, nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("submitted_by", String(255), nullable=False, server_default="legacy"),
            Column("archived_at", String(40)),
            Column("archived_by", String(255)),
            Column("archive_reason", String(1000)),
            Index("ix_knowledge_contract_proposals_active_submitter", "tenant_id", "connection_id", "submitted_by", "archived_at"),
        )
        self.source_grants = Table(
            "knowledge_source_grants",
            self.metadata,
            Column("tenant_id", String(64), primary_key=True),
            Column("connection_id", String(64), ForeignKey("knowledge_connections.id", ondelete="CASCADE"), primary_key=True),
            Column("principal_type", String(16), primary_key=True),
            Column("principal_id", String(255), primary_key=True),
            Column("role", String(16), nullable=False),
            Column("granted_at", String(40), nullable=False),
            Column("granted_by", String(255), nullable=False),
            Index("ix_knowledge_source_grants_lookup", "tenant_id", "principal_type", "principal_id", "role"),
        )
        self.refresh_schedules = Table(
            "knowledge_refresh_schedules", self.metadata,
            Column("id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            Column("connection_id", String(64), ForeignKey("knowledge_connections.id", ondelete="CASCADE"), nullable=False),
            Column("config", JSON, nullable=False),
            Column("paused", Boolean, nullable=False),
            Column("owner_subject", String(255), nullable=False),
            Column("created_at", String(40), nullable=False),
            Column("updated_at", String(40), nullable=False),
            Column("next_run_at", String(40)),
            Column("last_enqueued_at", String(40)),
            Column("last_job_id", String(64)),
            Column("last_error", String(40)),
            UniqueConstraint("tenant_id", "connection_id", name="uq_knowledge_refresh_schedules_source"),
            Index("ix_knowledge_refresh_schedules_ready", "paused", "next_run_at"),
        )
        self.refresh_jobs = Table(
            "knowledge_refresh_jobs", self.metadata,
            Column("id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            Column("connection_id", String(64), ForeignKey("knowledge_connections.id", ondelete="CASCADE"), nullable=False),
            Column("schedule_id", String(64), ForeignKey("knowledge_refresh_schedules.id", ondelete="SET NULL")),
            Column("status", String(20), nullable=False),
            Column("requested_by", String(255), nullable=False),
            Column("requested_at", String(40), nullable=False),
            Column("next_run_at", String(40), nullable=False),
            Column("started_at", String(40)),
            Column("finished_at", String(40)),
            Column("lease_until", String(40)),
            Column("worker_id", String(80)),
            Column("attempt", Integer, nullable=False),
            Column("max_attempts", Integer, nullable=False),
            Column("cancel_requested", Boolean, nullable=False),
            Column("result_snapshot_id", String(128)),
            Column("result_inventory_id", String(64)),
            Column("error_code", String(40)),
            Column("updated_at", String(40), nullable=False),
            Index("ix_knowledge_refresh_jobs_ready", "status", "next_run_at"),
            Index(
                "uq_knowledge_refresh_jobs_active_source", "tenant_id", "connection_id", unique=True,
                postgresql_where=text("status IN ('queued', 'running', 'retry_wait')"),
                sqlite_where=text("status IN ('queued', 'running', 'retry_wait')"),
            ),
        )
        self.publications = Table(
            "knowledge_source_publications", self.metadata,
            Column("tenant_id", String(64), primary_key=True),
            Column("connection_id", String(64), ForeignKey("knowledge_connections.id", ondelete="CASCADE"), primary_key=True),
            Column("snapshot_version", Integer, primary_key=True),
            Column("snapshot_id", String(128), nullable=False),
            Column("published_by", String(255), nullable=False),
            Column("published_at", String(40), nullable=False),
            Column("rationale", String(1000), nullable=False),
            Column("revoked_by", String(255)),
            Column("revoked_at", String(40)),
            Column("revoke_reason", String(1000)),
        )
        self.audit_events = Table(
            "knowledge_audit_events", self.metadata,
            Column("id", String(64), primary_key=True),
            Column("tenant_id", String(64), nullable=False),
            # Deliberately no FK: a source's audit trail must survive deletion.
            Column("connection_id", String(64)),
            Column("event_type", String(64), nullable=False),
            Column("actor", String(255), nullable=False),
            Column("occurred_at", String(40), nullable=False),
            Column("details", JSON, nullable=False),
            Index("ix_knowledge_audit_export", "tenant_id", "occurred_at", "id"),
            Index("ix_knowledge_audit_source", "tenant_id", "connection_id", "occurred_at"),
        )
        from knowledge_layer.guidance_schema import register_guidance_tables

        self.guidance_bundles, self.guidance_revisions = register_guidance_tables(self.metadata)
        from knowledge_layer.inventory_schema import inventory_tables

        for table_name, table in inventory_tables(self.metadata).items():
            setattr(self, table_name, table)
        from knowledge_layer.inventory_concept_schema import inventory_concept_tables

        for table_name, table in inventory_concept_tables(self.metadata).items():
            setattr(self, table_name, table)
        from knowledge_layer.autonomy_schema import autonomy_tables
        from knowledge_layer.build_schema import build_tables

        for table_name, table in {**autonomy_tables(self.metadata), **build_tables(self.metadata)}.items():
            setattr(self, table_name, table)
        if initialize_schema:
            # A versioned registry must upgrade explicitly. create_all would
            # otherwise create new tables before Alembic can apply their revision.
            try:
                if self.backend == "postgresql" and inspect(self.engine).has_table("alembic_version"):
                    from knowledge_layer.migrate import ensure_current_schema

                    ensure_current_schema(self)
                else:
                    self.metadata.create_all(self.engine)
            except Exception:
                self.engine.dispose()
                raise
        if initialize_schema and self.backend == "postgresql":
            # Existing POC databases may have the original 16-character column,
            # which cannot store the explicit discovery_complete state.
            with self.engine.begin() as conn:
                length = conn.exec_driver_sql(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'knowledge_snapshots' AND column_name = 'status'"
                ).scalar_one_or_none()
                if length is not None and length < 32:
                    conn.exec_driver_sql(
                        "ALTER TABLE knowledge_snapshots ALTER COLUMN status TYPE VARCHAR(32)"
                    )

    def close(self) -> None:
        self.engine.dispose()

    def _principal(self) -> Principal | None:
        if not self.access_enforced:
            return None
        principal = current_principal()
        if principal is None:
            raise KnowledgeAccessDenied("A verified request principal is required")
        return principal

    def _require_source_manager(self) -> Principal | None:
        principal = self._principal()
        if principal is not None and not (
            self.source_admin_group in principal.groups
            or "knowledge.sources.manage" in principal.scopes
        ):
            raise KnowledgeAccessDenied("Source management requires an operator-approved role")
        return principal

    def _require_source_approval(self, tenant: str, kind: str, config: dict[str, Any]) -> None:
        principal = self._principal()
        policy = self._current_source_policy()
        if policy is None:
            if self.access_enforced:
                raise KnowledgeAccessDenied("An operator source policy is required")
            return
        approved = (
            policy.approved(tenant, kind, config) if principal is None
            else policy.allows(tenant, kind, config, principal)
        )
        if not approved:
            raise KnowledgeAccessDenied("The underlying source is not approved for this principal")

    @property
    def source_policy(self) -> SourcePolicy | None:
        return self._source_policy

    @source_policy.setter
    def source_policy(self, policy: SourcePolicy | None) -> None:
        self._source_policy = policy
        if isinstance(policy, SourcePolicy) and policy.metadata_permissions.enforce:
            self._source_permissions_required = True

    def _current_source_policy(self) -> SourcePolicy | None:
        """Reload operator policy at authorization boundaries, never reuse failed rotation."""
        if self.source_policy_loader is not None:
            try:
                policy = self.source_policy_loader()
                if not isinstance(policy, SourcePolicy):
                    raise TypeError
                self.source_policy = policy
            except Exception:  # noqa: BLE001 - fail closed without credential-bearing loader diagnostics
                raise KnowledgeAccessDenied("Current operator source policy is unavailable") from None
        policy = self.source_policy
        if policy is not None and policy.metadata_permissions.enforce:
            self._source_permissions_required = True
        return policy

    def _require_source_identity_approval(self, tenant: str, kind: str, config: dict[str, Any]) -> None:
        """Read approval binds the source, not its separate management audience."""
        policy = self._current_source_policy()
        if policy is None:
            if self.access_enforced:
                raise KnowledgeAccessDenied("An operator source policy is required")
        elif not policy.approved(tenant, kind, config):
            raise KnowledgeAccessDenied("The underlying source is no longer approved")

    def _access_clause(self, tenant: str, connection_column, *, manage: bool = False):
        principal = self._principal()
        if principal is None:
            return true()
        grants = self.source_grants.c
        subjects = or_(
            and_(grants.principal_type == "user", grants.principal_id == principal.subject),
            and_(grants.principal_type == "group", grants.principal_id.in_(principal.groups))
            if principal.groups else false(),
        )
        conditions = [
            grants.tenant_id == tenant,
            grants.connection_id == connection_column,
            subjects,
        ]
        if manage:
            conditions.append(grants.role == "manage")
        grant = exists(select(grants.connection_id).where(*conditions))
        policy = self._current_source_policy()
        if not manage and self._source_permissions_required:
            if policy is None or not policy.metadata_permissions.enforce:
                return false()
            from knowledge_layer.source_permissions import access_clause

            return and_(grant, access_clause(self, tenant, connection_column,
                policy.metadata_permissions, principal, policy.approvals))
        return grant

    def _has_access(self, tenant: str, connection_id: str, *, manage: bool = False) -> bool:
        if not self.access_enforced:
            return True
        with self.engine.connect() as conn:
            return conn.execute(select(self._access_clause(tenant, connection_id, manage=manage))).scalar_one()

    def _require_access(self, tenant: str, connection_id: str, *, manage: bool = False) -> None:
        if not self._has_access(tenant, connection_id, manage=manage):
            raise KnowledgeNotFound("Connection was not found")

    def _lock_source(self, conn: Any, tenant: str, connection_id: str, *, manage: bool = True) -> dict:
        """Serialize grants, review, discovery and publication on the source row."""
        row = conn.execute(select(self.connections).where(
            self.connections.c.tenant_id == tenant, self.connections.c.id == connection_id,
            self._access_clause(tenant, self.connections.c.id, manage=manage),
        ).with_for_update()).mappings().first()
        if row is None:
            raise KnowledgeNotFound("Connection was not found")
        # A SELECT started before waiting for this lock can retain its earlier
        # grant snapshot. Read authorization again in a new statement after
        # acquisition; grant writers hold this same source lock until commit.
        current = conn.execute(select(self.connections.c.id).where(
            self.connections.c.tenant_id == tenant, self.connections.c.id == connection_id,
            self._access_clause(tenant, self.connections.c.id, manage=manage),
        )).scalar_one_or_none()
        if current is None:
            raise KnowledgeNotFound("Connection was not found")
        return dict(row)

    def _audit(
        self, conn, tenant: str, connection_id: str | None, event_type: str,
        actor: str, details: dict[str, Any],
    ) -> None:
        """Append a controlled event in the same transaction as its mutation."""
        conn.execute(insert(self.audit_events).values(
            id=f"audit_{uuid4().hex}", tenant_id=tenant, connection_id=connection_id,
            event_type=event_type, actor=actor, occurred_at=utc_now(), details=details,
        ))

    def audit_page(
        self, tenant_id: str, *, connection_id: str | None = None,
        after: tuple[str, str] | None = None, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Bounded operator-only export page, ordered by time and event ID."""
        if self.access_enforced:
            raise KnowledgeAccessDenied("Audit export requires an offline operator action")
        tenant = validate_tenant_id(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise KnowledgeValidationError("Audit page limit must be 1-1000")
        statement = select(self.audit_events).where(self.audit_events.c.tenant_id == tenant)
        if connection_id is not None:
            statement = statement.where(self.audit_events.c.connection_id == connection_id)
        if after is not None:
            if len(after) != 2:
                raise KnowledgeValidationError("Audit cursor is invalid")
            statement = statement.where(or_(
                self.audit_events.c.occurred_at > after[0],
                and_(self.audit_events.c.occurred_at == after[0], self.audit_events.c.id > after[1]),
            ))
        statement = statement.order_by(self.audit_events.c.occurred_at, self.audit_events.c.id).limit(limit)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(statement).mappings()]

    def iter_audit_events(
        self, tenant_id: str, *, connection_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream one consistent operator-only export snapshot in bounded batches."""
        if self.access_enforced:
            raise KnowledgeAccessDenied("Audit export requires an offline operator action")
        tenant = validate_tenant_id(tenant_id)
        statement = select(self.audit_events).where(self.audit_events.c.tenant_id == tenant)
        if connection_id is not None:
            statement = statement.where(self.audit_events.c.connection_id == connection_id)
        statement = statement.order_by(self.audit_events.c.occurred_at, self.audit_events.c.id)
        with self.engine.connect() as conn:
            if self.backend == "postgresql":
                conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                rows = conn.execution_options(stream_results=True, yield_per=500).execute(statement)
                for row in rows.mappings():
                    yield dict(row)

    def grant_source_access(
        self, tenant_id: str, connection_id: str, *, principal_type: str,
        principal_id: str, role: str, granted_by: str = "operator",
    ) -> None:
        """Operator-only grant; OIDC requests cannot broaden source visibility."""
        if self.access_enforced:
            raise KnowledgeAccessDenied("Source grants require an operator action")
        tenant = validate_tenant_id(tenant_id)
        if principal_type not in {"user", "group"} or role not in {"read", "manage"}:
            raise KnowledgeValidationError("Source grant type or role is invalid")
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 255:
            raise KnowledgeValidationError("Source grant principal must be 1-255 characters")
        if not isinstance(granted_by, str) or not granted_by or len(granted_by) > 255:
            raise KnowledgeValidationError("Source grant actor must be 1-255 characters")
        if self.get_connection(tenant, connection_id) is None:
            raise KnowledgeNotFound("Connection was not found")
        key = (
            self.source_grants.c.tenant_id == tenant,
            self.source_grants.c.connection_id == connection_id,
            self.source_grants.c.principal_type == principal_type,
            self.source_grants.c.principal_id == principal_id,
        )
        with self.engine.begin() as conn:
            self._lock_source(conn, tenant, connection_id)
            previous = conn.execute(select(self.source_grants.c.role).where(*key)).scalar_one_or_none()
            if previous is not None:
                conn.execute(update(self.source_grants).where(*key).values(
                    role=role, granted_at=utc_now(), granted_by=granted_by,
                ))
            else:
                conn.execute(insert(self.source_grants).values(
                    tenant_id=tenant, connection_id=connection_id,
                    principal_type=principal_type, principal_id=principal_id,
                    role=role, granted_at=utc_now(), granted_by=granted_by,
                ))
            self._audit(conn, tenant, connection_id, "source.grant", granted_by, {
                "principal_type": principal_type, "principal_id": principal_id,
                "previous_role": previous, "role": role,
            })

    def revoke_source_access(
        self, tenant_id: str, connection_id: str, *, principal_type: str, principal_id: str,
        revoked_by: str = "operator",
    ) -> None:
        if self.access_enforced:
            raise KnowledgeAccessDenied("Source grants require an operator action")
        tenant = validate_tenant_id(tenant_id)
        if not isinstance(revoked_by, str) or not revoked_by or len(revoked_by) > 255:
            raise KnowledgeValidationError("Source revoke actor must be 1-255 characters")
        with self.engine.begin() as conn:
            self._lock_source(conn, tenant, connection_id)
            key = (
                self.source_grants.c.tenant_id == tenant,
                self.source_grants.c.connection_id == connection_id,
                self.source_grants.c.principal_type == principal_type,
                self.source_grants.c.principal_id == principal_id,
            )
            previous = conn.execute(select(self.source_grants.c.role).where(*key)).scalar_one_or_none()
            if previous is not None:
                conn.execute(delete(self.source_grants).where(*key))
                self._audit(conn, tenant, connection_id, "source.revoke_grant", revoked_by, {
                    "principal_type": principal_type, "principal_id": principal_id,
                    "previous_role": previous,
                })

    def connector_types(self) -> list[dict[str, Any]]:
        """Return connector schemas plus only existing, contained demo folders."""
        from knowledge_layer.curation import CONNECTORS

        result = deepcopy([item for key, item in CONNECTOR_TYPES.items() if key in CONNECTORS])
        presets = (("juniper", "Juniper Market"), ("northstar", "Northstar Field Services"))
        for connector in result:
            if connector["id"] != "local_files":
                continue
            path_field = next(field for field in connector["config_fields"] if field["name"] == "path")
            options = []
            for relative, label in presets:
                candidate = self.local_files_root / relative
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if candidate.is_symlink() or not resolved.is_dir() or not resolved.is_relative_to(self.local_files_root):
                    continue
                options.append({"label": label, "value": relative})
            path_field["options"] = options
        return result

    def create_connection(
        self,
        tenant_id: str,
        *,
        kind: str,
        name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        tenant = validate_tenant_id(tenant_id)
        creator = self._require_source_manager()
        normalized_config = validate_connection_config(kind, config)
        self._require_source_approval(tenant, kind, normalized_config)
        display_name = _validate_name(name)
        now = utc_now()
        connection_id = f"conn_{uuid4().hex}"
        row = {
            "id": connection_id,
            "tenant_id": tenant,
            "kind": kind,
            "name": display_name,
            "config": normalized_config,
            "status": "configured",
            "created_at": now,
            "updated_at": now,
            "last_refreshed_at": None,
            "last_error": None,
        }
        with self.engine.begin() as conn:
            conn.execute(insert(self.connections).values(**row))
            self._audit(conn, tenant, connection_id, "source.create",
                        creator.subject if creator else "operator", {"kind": kind})
            if creator is not None:
                conn.execute(insert(self.source_grants).values(
                    tenant_id=tenant, connection_id=connection_id,
                    principal_type="user", principal_id=creator.subject,
                    role="manage", granted_at=now, granted_by=creator.subject,
                ))
                self._audit(conn, tenant, connection_id, "source.grant", creator.subject, {
                    "principal_type": "user", "principal_id": creator.subject,
                    "previous_role": None, "role": "manage",
                })
                if self.local_agent_subject is not None and creator.subject == "local-owner":
                    conn.execute(insert(self.source_grants).values(
                        tenant_id=tenant, connection_id=connection_id, principal_type="user",
                        principal_id=self.local_agent_subject, role="read", granted_at=now,
                        granted_by=creator.subject,
                    ))
                    self._audit(conn, tenant, connection_id, "source.grant", creator.subject, {
                        "principal_type": "user", "principal_id": self.local_agent_subject,
                        "previous_role": None, "role": "read",
                    })
        return self._connection_response(row, snapshot=None)

    def _connection_query(self, tenant: str, cursor: str | None):
        statement = select(self.connections).where(
            self.connections.c.tenant_id == tenant,
            self._access_clause(tenant, self.connections.c.id),
        )
        if cursor is not None:
            created_at, connection_id = _decode_connection_cursor(tenant, cursor)
            statement = statement.where(or_(
                self.connections.c.created_at < created_at,
                and_(self.connections.c.created_at == created_at, self.connections.c.id < connection_id),
            ))
        return statement.order_by(self.connections.c.created_at.desc(), self.connections.c.id.desc())

    def list_connections(
        self, tenant_id: str = "demo", *, limit: int | None = None, cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        tenant = validate_tenant_id(tenant_id)
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise KnowledgeValidationError("connection limit must be 1-1000")
        if cursor is not None and limit is None:
            raise KnowledgeValidationError("A limit is required with a connection cursor")
        statement = self._connection_query(tenant, cursor)
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(statement).mappings().all()
        return self._connection_responses_for_rows(tenant, rows)

    def iter_connections(self, tenant_id: str = "demo", *, batch_size: int = 100):
        """Stream authorized source summaries without retaining the full registry."""
        tenant = validate_tenant_id(tenant_id)
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise KnowledgeValidationError("connection batch size must be 1-1000")
        cursor: str | None = None
        while True:
            batch = self.list_connections(tenant, limit=batch_size, cursor=cursor)
            if not batch:
                return
            yield from batch
            if len(batch) < batch_size:
                return
            cursor = _encode_connection_cursor(tenant, batch[-1])

    def search_concept_candidates(
        self, tenant_id: str, query: str, *, limit: int = 256,
    ) -> list[dict[str, Any]]:
        """Fetch grant-filtered current records with bounded projection and SQL work time."""
        tenant = validate_tenant_id(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise KnowledgeValidationError("concept candidate limit must be 1-256")
        indexed = self.concept_search.c
        connections = self.connections.c
        base_statement = select(self.concept_search, connections.name.label("connection_name")).join(
            self.connections, and_(connections.tenant_id == indexed.tenant_id,
                                   connections.id == indexed.connection_id),
        ).where(
            indexed.tenant_id == tenant,
            self._access_clause(tenant, indexed.connection_id),
        )
        if self.backend == "postgresql":
            vector = func.to_tsvector("simple", indexed.search_text)
            tsquery = func.plainto_tsquery("simple", query)
            statement = base_statement.where(vector.op("@@")(tsquery)).order_by(
                (func.lower(indexed.name) == query.casefold()).desc(),
                func.ts_rank_cd(vector, tsquery).desc(), indexed.connection_id, indexed.concept_id,
            )
        else:
            statement = base_statement.where(func.lower(indexed.search_text).like(
                f"%{query.casefold().replace('%', r'\%').replace('_', r'\_')}%", escape="\\",
            )).order_by(indexed.connection_id, indexed.concept_id)
        with self.engine.begin() as conn:
            if self.backend == "postgresql":
                conn.exec_driver_sql("SET LOCAL statement_timeout = '1500ms'")
            rows = conn.execute(statement.limit(limit)).mappings().all()
            question_like = bool(re.search(r"\?\s*$", query)) or bool(re.match(
                r"\s*(?:how|what|which|can|could|does|do|is|are|why|where)\b", query,
                flags=re.IGNORECASE))
            if not rows and question_like:
                # A question rarely appears verbatim in a concept record. Keep
                # exact-phrase lookup for named terms, then retrieve a bounded
                # question candidate set by meaningful words for relevance ranking.
                words = list(dict.fromkeys(re.findall(r"[a-z0-9_]{3,}", query.casefold())))
                stop = {"about", "across", "after", "before", "could", "does", "from", "have",
                        "into", "show", "that", "their", "these", "this", "what", "when",
                        "where", "which", "with", "would"}
                words = sorted((word for word in words if word not in stop), key=lambda word: -len(word))[:6]
                if len(words) > 1:
                    if self.backend == "postgresql":
                        broad = func.to_tsquery("simple", " | ".join(words))
                        statement = base_statement.where(vector.op("@@")(broad)).order_by(
                            func.ts_rank_cd(vector, broad).desc(), indexed.connection_id, indexed.concept_id)
                    else:
                        matches = [func.lower(indexed.search_text).like(
                            "%" + word.replace("_", "\\_") + "%", escape="\\",
                        ) for word in words]
                        score = sum(func.cast(match, Integer) for match in matches)
                        statement = base_statement.where(or_(*matches)).order_by(
                            score.desc(), indexed.connection_id, indexed.concept_id)
                    rows = conn.execute(statement.limit(limit)).mappings().all()
        result = [dict(row) for row in rows]
        for source_id in {row["connection_id"] for row in result}:
            record_source_read(tenant, source_id)
        return result

    def iter_current_evidence_sources(self, tenant_id: str = "demo"):
        """Stream current source evidence under the same source grant boundary."""
        tenant = validate_tenant_id(tenant_id)
        snapshots = self.snapshots.c
        ranked = select(
            snapshots.connection_id,
            snapshots.snapshot_id,
            snapshots.payload,
            func.row_number().over(
                partition_by=snapshots.connection_id,
                order_by=snapshots.version.desc(),
            ).label("source_rank"),
        ).where(snapshots.tenant_id == tenant).subquery()
        statement = select(
            self.connections.c.id,
            self.connections.c.name,
            ranked.c.snapshot_id,
            ranked.c.payload["evidence"].label("evidence"),
        ).join(ranked, and_(
            ranked.c.connection_id == self.connections.c.id,
            ranked.c.source_rank == 1,
        )).where(
            self.connections.c.tenant_id == tenant,
            self._access_clause(tenant, self.connections.c.id),
        )
        with self.engine.connect().execution_options(yield_per=20) as conn:
            for row in conn.execute(statement).mappings():
                record_source_read(tenant, row["id"])
                yield {"connection_id": row["id"], "connection_name": row["name"],
                       "snapshot_id": row["snapshot_id"], "evidence": row["evidence"] or []}

    def page_connections(
        self, tenant_id: str = "demo", *, limit: int = 30, cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded authorized page with a stable, tenant-bound seek cursor."""
        tenant = validate_tenant_id(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise KnowledgeValidationError("connection page limit must be 1-100")
        with self.engine.connect() as conn:
            rows = conn.execute(self._connection_query(tenant, cursor).limit(limit + 1)).mappings().all()
        page = self._connection_responses_for_rows(tenant, rows[:limit])
        return {
            "connections": page,
            "next_cursor": _encode_connection_cursor(tenant, page[-1]) if len(rows) > limit else None,
            "total": self.count_connections(tenant),
        }

    def _connection_responses_for_rows(self, tenant: str, rows: list[Any]) -> list[dict[str, Any]]:
        """Project current source state in bounded batches, preserving grant checks."""
        result: list[dict[str, Any]] = []
        for offset in range(0, len(rows), 20):
            chunk = rows[offset:offset + 20]
            snapshots = self._current_snapshots_for_rows(tenant, chunk)
            for row in chunk:
                record_source_read(tenant, row["id"])
                result.append(self._connection_response(dict(row), snapshot=snapshots.get(row["id"])))
        return result

    def _current_snapshots_for_rows(self, tenant: str, connections: list[Any]) -> dict[str, dict[str, Any]]:
        if not connections:
            return {}
        ids = [row["id"] for row in connections]
        snapshots = self.snapshots.c
        ranked = select(
            *self.snapshots.c,
            func.row_number().over(
                partition_by=snapshots.connection_id,
                order_by=snapshots.version.desc(),
            ).label("page_rank"),
        ).where(
            snapshots.tenant_id == tenant,
            snapshots.connection_id.in_(ids),
            self._access_clause(tenant, snapshots.connection_id),
        ).subquery()
        with self.engine.connect() as conn:
            snapshot_rows = {
                row["connection_id"]: dict(row)
                for row in conn.execute(select(ranked).where(ranked.c.page_rank == 1)).mappings()
            }
        if not snapshot_rows:
            return {}
        active_ids = list(snapshot_rows)
        proposals: dict[str, list[dict[str, Any]]] = {source_id: [] for source_id in active_ids}
        recorded: dict[str, dict[str, dict[str, Any]]] = {source_id: {} for source_id in active_ids}
        local_ids: dict[str, set[str]] = {source_id: set() for source_id in active_ids}
        latest_decision: dict[str, str] = {}
        max_batch_rows = (MAX_SOURCE_PROPOSALS + 1) * len(active_ids)
        with self.engine.connect() as conn:
            for table, serialize in (
                (self.proposals, _semantic_proposal_response),
                (self.contract_proposals, _contract_proposal_response),
            ):
                rows = conn.execute(select(table).where(
                    table.c.tenant_id == tenant,
                    table.c.connection_id.in_(active_ids),
                    table.c.archived_at.is_(None),
                ).order_by(table.c.connection_id, table.c.created_at, table.c.id)
                    .limit(max_batch_rows + 1)).mappings().all()
                if len(rows) > max_batch_rows:
                    raise KnowledgeConflict("Source proposal ledger exceeds the supported limit")
                by_source: dict[str, int] = {}
                for row in rows:
                    source_id = row["connection_id"]
                    by_source[source_id] = by_source.get(source_id, 0) + 1
                    if by_source[source_id] > MAX_SOURCE_PROPOSALS:
                        raise KnowledgeConflict("Source proposal ledger exceeds the supported limit")
                    proposals[source_id].append(serialize(dict(row)))
            for decision in conn.execute(select(self.decisions).where(
                self.decisions.c.tenant_id == tenant,
                self.decisions.c.connection_id.in_(active_ids),
            )).mappings():
                source_id = decision["connection_id"]
                recorded[source_id][decision["proposal_id"]] = dict(decision)
                previous = latest_decision.get(source_id)
                if previous is None or decision["decided_at"] > previous:
                    latest_decision[source_id] = decision["decided_at"]
            candidate_ids = list(dict.fromkeys(
                _record_id(item)
                for row in snapshot_rows.values()
                for item in (row["payload"] or {}).get("proposals", [])
                if isinstance(item, dict) and _record_id(item)
            ))
            for offset in range(0, len(candidate_ids), 500):
                candidate_chunk = candidate_ids[offset:offset + 500]
                for table in (self.proposals, self.contract_proposals):
                    for source_id, proposal_id in conn.execute(select(table.c.connection_id, table.c.id).where(
                        table.c.tenant_id == tenant,
                        table.c.connection_id.in_(active_ids),
                        table.c.id.in_(candidate_chunk),
                    )):
                        local_ids[source_id].add(proposal_id)
            publication_predicates = [and_(
                self.publications.c.connection_id == source_id,
                self.publications.c.snapshot_version == row["version"],
            ) for source_id, row in snapshot_rows.items()]
            publication_rows = {
                row["connection_id"]: dict(row)
                for row in conn.execute(select(
                    self.publications,
                    self.connections.c.status.label("source_status"),
                ).join(self.connections, and_(
                    self.connections.c.tenant_id == self.publications.c.tenant_id,
                    self.connections.c.id == self.publications.c.connection_id,
                )).where(
                    self.publications.c.tenant_id == tenant,
                    self.publications.c.revoked_at.is_(None),
                    or_(*publication_predicates),
                )).mappings()
            }
            refresh_active = set(conn.execute(select(self.refresh_jobs.c.connection_id).where(
                self.refresh_jobs.c.tenant_id == tenant,
                self.refresh_jobs.c.connection_id.in_(active_ids),
                self.refresh_jobs.c.status.in_(("queued", "running", "retry_wait")),
            )).scalars())
        result: dict[str, dict[str, Any]] = {}
        for connection in connections:
            source_id = connection["id"]
            row = snapshot_rows.get(source_id)
            if row is None:
                continue
            snapshot = self._project_snapshot_row(
                tenant, source_id, row, proposals[source_id], local_ids[source_id],
                recorded_decisions=recorded[source_id],
            )
            publication = publication_rows.get(source_id)
            if publication:
                self._apply_publication_state(snapshot, row, {
                    "source_status": publication["source_status"],
                    "snapshot_id": publication["snapshot_id"],
                    "snapshot_version": publication["snapshot_version"],
                    "published_by": publication["published_by"],
                    "published_at": publication["published_at"],
                    "rationale": publication["rationale"],
                    "refresh_active": source_id in refresh_active,
                    "latest_decision_at": latest_decision.get(source_id),
                })
            result[source_id] = snapshot
        return result

    def count_connections(self, tenant_id: str = "demo") -> int:
        tenant = validate_tenant_id(tenant_id)
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(self.connections).where(
                self.connections.c.tenant_id == tenant,
                self._access_clause(tenant, self.connections.c.id),
            )).scalar_one())

    def get_connection(self, tenant_id: str, connection_id: str) -> dict[str, Any] | None:
        tenant = validate_tenant_id(tenant_id)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self.connections).where(
                    self.connections.c.tenant_id == tenant,
                    self.connections.c.id == connection_id,
                    self._access_clause(tenant, self.connections.c.id),
                )
            ).mappings().first()
        if row is None:
            return None
        record_source_read(tenant, row["id"])
        snapshot = self.get_snapshot(tenant, connection_id)
        return self._connection_response(dict(row), snapshot=snapshot)

    def _management_connection(self, tenant: str, connection_id: str) -> dict[str, Any]:
        """Private ingestion input for an authorized manager repairing hidden metadata."""
        with self.engine.begin() as conn:
            row = self._lock_source(conn, tenant, connection_id, manage=True)
            self._require_source_approval(tenant, row["kind"], row["config"])
            snapshot = conn.execute(select(self.snapshots).where(
                self.snapshots.c.tenant_id == tenant,
                self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).mappings().first()
        previous = deepcopy(snapshot["payload"]) if snapshot is not None else None
        if previous is not None:
            previous.update({"id": snapshot["snapshot_id"], "version": snapshot["version"],
                             "created_at": snapshot["created_at"], "status": snapshot["status"]})
        return {**row, "snapshot": previous}

    def _management_source(self, tenant_id: str, connection_id: str) -> dict[str, Any]:
        """Private selector input; never return this row through a public API."""
        tenant = validate_tenant_id(tenant_id)
        self._require_source_manager()
        with self.engine.begin() as conn:
            row = self._lock_source(conn, tenant, connection_id, manage=True)
            self._require_source_approval(tenant, row["kind"], row["config"])
        return dict(row)

    def _refresh_response(self, tenant: str, connection_id: str, *, failed: bool = False) -> dict[str, Any]:
        current = self.get_connection(tenant, connection_id)
        if current is None:
            # Mutation acknowledgement is safe; no source text or history leaves
            # the private manager ingestion context when read authority is absent.
            return {"connection": {"id": connection_id, "status": "error" if failed else "refresh_complete",
                                   "metadata_unavailable": True}, "snapshot": None}
        return {"connection": current, "snapshot": current.get("snapshot")}

    def find_connection_by_name(self, tenant_id: str, *, kind: str, name: str) -> dict[str, Any] | None:
        """Find one possible retry target without loading every source snapshot."""
        tenant = validate_tenant_id(tenant_id)
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self.connections).where(
                    self.connections.c.tenant_id == tenant,
                    self.connections.c.kind == kind,
                    func.lower(self.connections.c.name) == name.strip().lower(),
                    self._access_clause(tenant, self.connections.c.id),
                ).order_by(self.connections.c.created_at.desc()).limit(1)
            ).mappings().first()
        if row is None:
            return None
        record_source_read(tenant, row["id"])
        return self._connection_response(dict(row), snapshot=self.get_snapshot(tenant, row["id"]))

    def _find_management_source_by_name(self, tenant_id: str, *, kind: str, name: str) -> dict[str, Any] | None:
        """Private retry selector without reading or returning native metadata."""
        tenant = validate_tenant_id(tenant_id)
        self._require_source_manager()
        with self.engine.connect() as conn:
            row = conn.execute(select(self.connections).where(
                self.connections.c.tenant_id == tenant, self.connections.c.kind == kind,
                func.lower(self.connections.c.name) == name.strip().lower(),
                self._access_clause(tenant, self.connections.c.id, manage=True),
            ).order_by(self.connections.c.created_at.desc()).limit(1)).mappings().first()
        if row is None:
            return None
        self._require_source_approval(tenant, row["kind"], row["config"])
        return dict(row)

    def update_connection_config(self, tenant_id: str, connection_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Replace validated selectors while preserving the connection's history."""
        tenant = validate_tenant_id(tenant_id)
        self._require_source_manager()
        self._require_access(tenant, connection_id, manage=True)
        current = self._management_source(tenant, connection_id)
        normalized = validate_connection_config(current["kind"], config)
        if (self.access_enforced or self.source_policy is not None) and (
            source_identity(current["kind"], normalized)
            != source_identity(current["kind"], current["config"])
        ):
            raise KnowledgeAccessDenied("Changing a source identity requires a new connection and grant")
        self._require_source_approval(tenant, current["kind"], normalized)
        if normalized != current["config"]:
            with self.engine.begin() as conn:
                locked = self._lock_source(conn, tenant, connection_id)
                if locked["config"] != current["config"]:
                    raise KnowledgeConflict("Source configuration changed; fetch the current source before updating")
                conn.execute(
                    update(self.connections)
                    .where(self.connections.c.tenant_id == tenant, self.connections.c.id == connection_id)
                    .values(config=normalized, status="configured", updated_at=utc_now(), last_error=None)
                )
                principal = current_principal()
                self._audit(conn, tenant, connection_id, "source.configure",
                            principal.subject if principal else "operator", {"kind": current["kind"]})
        return self._refresh_response(tenant, connection_id)["connection"]

    def get_snapshot(self, tenant_id: str, connection_id: str, version: int | None = None) -> dict[str, Any] | None:
        from knowledge_layer.cross_source import dependency_projection

        with dependency_projection():
            return self._get_snapshot(tenant_id, connection_id, version=version)

    def _get_snapshot(self, tenant_id: str, connection_id: str, version: int | None = None) -> dict[str, Any] | None:
        tenant = validate_tenant_id(tenant_id)
        if not self._has_access(tenant, connection_id):
            return None
        statement = select(self.snapshots).where(
                    self.snapshots.c.tenant_id == tenant,
                    self.snapshots.c.connection_id == connection_id,
                )
        if version is None:
            statement = statement.order_by(self.snapshots.c.version.desc()).limit(1)
        else:
            statement = statement.where(self.snapshots.c.version == version).limit(1)
        with self.engine.connect() as conn:
            row = conn.execute(statement).mappings().first()
        if row is None:
            return None
        version_as_of = None if version is None else str(row["created_at"])
        local_proposals = self._list_local_proposals(tenant, connection_id, as_of=version_as_of)
        local_proposals.extend(self._list_contract_proposals(tenant, connection_id, as_of=version_as_of))
        local_ids = self._local_proposal_ids(
            tenant, connection_id,
            [_record_id(item) for item in (row["payload"] or {}).get("proposals", []) if isinstance(item, dict)],
        )
        result = self._project_snapshot_row(
            tenant, connection_id, row, local_proposals, local_ids, as_of=version_as_of,
        )
        if version is None:
            active_refresh = exists(select(self.refresh_jobs.c.id).where(
                self.refresh_jobs.c.tenant_id == tenant,
                self.refresh_jobs.c.connection_id == connection_id,
                self.refresh_jobs.c.status.in_(("queued", "running", "retry_wait")),
            ))
            latest_decision_at = select(func.max(self.decisions.c.decided_at)).where(
                self.decisions.c.tenant_id == tenant,
                self.decisions.c.connection_id == connection_id,
            ).scalar_subquery()
            with self.engine.connect() as conn:
                state = conn.execute(select(
                    self.connections.c.status.label("source_status"),
                    self.publications.c.snapshot_id,
                    self.publications.c.snapshot_version,
                    self.publications.c.published_by,
                    self.publications.c.published_at,
                    self.publications.c.rationale,
                    active_refresh.label("refresh_active"),
                    latest_decision_at.label("latest_decision_at"),
                ).select_from(self.connections.outerjoin(self.publications, and_(
                    self.publications.c.tenant_id == tenant,
                    self.publications.c.connection_id == connection_id,
                    self.publications.c.snapshot_version == row["version"],
                    self.publications.c.revoked_at.is_(None),
                ))).where(
                    self.connections.c.tenant_id == tenant,
                    self.connections.c.id == connection_id,
                )).mappings().first()
            self._apply_publication_state(result, row, state)
        return result

    def _project_snapshot_row(
        self, tenant: str, connection_id: str, row: Any,
        local_proposals: list[dict[str, Any]], local_ids: set[str], *,
        as_of: str | None = None, recorded_decisions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        record_source_read(tenant, connection_id)
        # Compose an owned top-level input. _apply_decisions deep-copies the
        # complete payload, including local proposals, before any nested edits.
        snapshot = dict(row["payload"])
        snapshot.update({"id": row["snapshot_id"], "version": row["version"],
                         "created_at": row["created_at"], "status": row["status"]})
        # Reviewed snapshots can embed earlier local proposals. Archival must
        # hide those copies too, without deleting the versioned raw payload.
        source_proposals = [item for item in snapshot.get("proposals", [])
                            if isinstance(item, dict) and _record_id(item) not in local_ids]
        snapshot["proposals"] = source_proposals + local_proposals
        result = self._apply_decisions(
            tenant, connection_id, snapshot, as_of=as_of,
            recorded_decisions=recorded_decisions,
        )
        # A grant on this source does not grant access to referenced sources.
        # Remove whole drafts, including copied schema and source identities.
        # Preserve only an opaque blocker when an approved dependency is hidden.
        visible_proposals = []
        for proposal in result.get("proposals", []):
            refs = proposal.get("evidence_refs", []) if isinstance(proposal, dict) else []
            if any(not self._has_access(tenant, ref.get("connection_id", "")) for ref in refs):
                if proposal.get("decision") == "approve":
                    result["dependency_unavailable"] = True
                continue
            visible_proposals.append(proposal)
        result["proposals"] = visible_proposals
        # Connector payloads and legacy rows cannot grant publication authority.
        result["agent_ready"] = False
        result["meaning_certified"] = False
        if result["status"] == "ready":
            result["status"] = "discovery_complete"
        return result

    def _apply_publication_state(self, result: dict[str, Any], row: Any, state: Any) -> None:
        if not state or state["snapshot_id"] != row["snapshot_id"]:
            return
        active = (state["source_status"] == "discovery_complete" and not state["refresh_active"]
                  and not result.get("errors") and not result.get("review_integrity_failed")
                  and not result.get("dependency_unavailable")
                  and (state["latest_decision_at"] is None
                       or state["latest_decision_at"] <= state["published_at"])
                  and not self._has_stale_approved(result))
        result["publication"] = {
            "snapshot_id": state["snapshot_id"],
            "snapshot_version": state["snapshot_version"],
            "published_by": state["published_by"],
            "published_at": state["published_at"],
            "rationale": state["rationale"],
            "active": active,
        }
        if active:
            result["agent_ready"] = True
            result["status"] = "ready"
            result["curation_status"] = "published_for_agents"

    def _replace_concept_search(self, conn, tenant: str, source_id: str,
                                version: int, payload: dict[str, Any]) -> None:
        """Keep the current search rows in the source mutation transaction."""
        from knowledge_layer.concept_index import records as concept_records

        approved = set(conn.execute(select(self.decisions.c.proposal_id).where(
            self.decisions.c.tenant_id == tenant,
            self.decisions.c.connection_id == source_id,
            self.decisions.c.decision == "approve",
        )).scalars())
        embedded_ids = [_record_id(item) for item in payload.get("proposals") or []
                        if isinstance(item, dict) and _record_id(item)]
        archived_local: set[str] = set()
        for offset in range(0, len(embedded_ids), 500):
            archived_local.update(conn.execute(select(self.proposals.c.id).where(
                self.proposals.c.tenant_id == tenant,
                self.proposals.c.connection_id == source_id,
                self.proposals.c.id.in_(embedded_ids[offset:offset + 500]),
                self.proposals.c.archived_at.is_not(None),
            )).scalars())
        alternate_terms: dict[str, list[str]] = defaultdict(list)
        for proposal in payload.get("proposals") or []:
            if (not isinstance(proposal, dict) or _record_id(proposal) not in approved
                    or _record_id(proposal) in archived_local):
                continue
            target_id, definition = proposal.get("target_concept_id"), proposal.get("proposed_definition")
            if target_id and isinstance(definition, str):
                alternate_terms[str(target_id)].append(definition[:400])
        for target_id, definition in conn.execute(select(
            self.proposals.c.target_concept_id, self.proposals.c.proposed_definition,
        ).join(self.decisions, and_(
            self.decisions.c.tenant_id == self.proposals.c.tenant_id,
            self.decisions.c.connection_id == self.proposals.c.connection_id,
            self.decisions.c.proposal_id == self.proposals.c.id,
            self.decisions.c.decision == "approve",
        )).where(
            self.proposals.c.tenant_id == tenant,
            self.proposals.c.connection_id == source_id,
            self.proposals.c.archived_at.is_(None),
            self.proposals.c.proposed_definition.is_not(None),
        )):
            alternate_terms[str(target_id)].append(str(definition)[:400])
        conn.execute(delete(self.concept_search).where(
            self.concept_search.c.tenant_id == tenant,
            self.concept_search.c.connection_id == source_id,
        ))
        index_payload = deepcopy(payload)
        for concept in index_payload.get("concepts") or []:
            if not isinstance(concept, dict):
                continue
            marker = concept.get("definition_proposal_id")
            if marker and (marker not in approved or marker in archived_local):
                concept.pop("definition", None)
        indexed = concept_records(index_payload)
        for offset in range(0, len(indexed), 500):
            conn.execute(insert(self.concept_search), [
                {"row_id": uuid4().hex, "tenant_id": tenant, "connection_id": source_id,
                 "snapshot_version": version, "concept_id": record["concept_id"],
                 "name": record["name"], "definition": record["definition"],
                 "semantic_type": record["semantic_type"], "trust_state": record["trust_state"],
                 "evidence_ids": record["evidence_ids"], "evidence": record["evidence"],
                 "search_text": (record["text"] + " " +
                                 " ".join(alternate_terms[record["concept_id"]])[:1000])[:2001]}
                for record in indexed[offset:offset + 500]
            ])

    def save_snapshot(
        self, tenant_id: str, connection_id: str, payload: dict[str, Any], *,
        source_refresh: bool = False,
    ) -> dict[str, Any]:
        """Persist a sanitized source snapshot for an existing tenant connection."""
        tenant = validate_tenant_id(tenant_id)
        self._require_source_manager()
        self._require_access(tenant, connection_id, manage=True)
        clean, status, snapshot_id = self._prepare_snapshot(payload)
        with self.engine.begin() as conn:
            self._save_snapshot_in_transaction(
                conn, tenant, connection_id, clean, status=status,
                snapshot_id=snapshot_id, source_refresh=source_refresh,
            )
        return self.get_snapshot(tenant, connection_id) or {}

    def _save_review_snapshot(self, tenant: str, connection_id: str, payload: dict[str, Any],
                              *, parent: tuple[str, int]) -> dict[str, Any]:
        """Only local steward annotations inherit the locked native resource set."""
        self._require_source_manager()
        self._require_access(tenant, connection_id, manage=True)
        clean, status, snapshot_id = self._prepare_snapshot(payload)
        with self.engine.begin() as conn:
            self._save_snapshot_in_transaction(conn, tenant, connection_id, clean,
                status=status, snapshot_id=snapshot_id, review_parent=parent)
        return self.get_snapshot(tenant, connection_id) or {}

    @staticmethod
    def _prepare_snapshot(payload: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        """Sanitize a new snapshot identically for source refresh and human review."""
        if not isinstance(payload, dict):
            raise KnowledgeValidationError("curation snapshot must be an object")
        clean = _sanitize({key: value for key, value in payload.items() if key in _ALLOWED_SNAPSHOT_FIELDS})
        for field in ("assets", "concepts", "relationships", "proposals", "metrics", "evidence", "errors"):
            clean[field] = clean.get(field) if isinstance(clean.get(field), list) else []
        for field in ("source", "drift", "capabilities"):
            clean[field] = clean.get(field) if isinstance(clean.get(field), dict) else {}
        status = str(clean.get("status") or "partial").lower()
        if status == "ready":
            status = "partial" if clean.get("discovery_complete") is False else "discovery_complete"
            clean["discovery_complete"] = status == "discovery_complete"
            clean["curation_status"] = "candidate_review_required"
        if status == "discovery_complete" and clean.get("discovery_complete") is False:
            status = "partial"
        if status not in {"ready", "partial", "error", "discovery_complete"}:
            status = "partial"
        clean["meaning_certified"] = False
        clean["agent_ready"] = False
        if clean.get("curation_status") == "published_for_agents":
            clean["curation_status"] = "candidate_review_required"
        for collection in ("assets", "concepts", "relationships", "proposals", "metrics"):
            for item in clean[collection]:
                if not isinstance(item, dict):
                    continue
                if "certified" in item:
                    item["certified"] = False
                if item.get("status") == "approved":
                    item["status"] = "candidate"
                if collection == "concepts":
                    if item.get("definition_status") == "approved":
                        item["definition_status"] = "candidate"
                    if item.get("mapping_status") == "approved":
                        item["mapping_status"] = "candidate"
                    item.pop("approved_mapping", None)
        snapshot_id = str(clean.get("snapshot_id") or f"snapshot_{uuid4().hex}")[:128]
        serialized_size = len(json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if serialized_size > 5 * 1024 * 1024:
            raise KnowledgeValidationError("curation snapshot exceeds the 5 MiB metadata limit")
        from knowledge_layer.source_permissions import (
            SNAPSHOT_MANIFEST_VERSION,
            snapshot_resource_manifest,
        )

        clean["source_permission_manifest_digest"] = snapshot_resource_manifest(clean)
        clean["source_permission_manifest_version"] = SNAPSHOT_MANIFEST_VERSION
        return clean, status, snapshot_id

    def _save_snapshot_in_transaction(
        self, conn: Any, tenant: str, connection_id: str, clean: dict[str, Any], *,
        status: str, snapshot_id: str, source_refresh: bool = False,
        review_parent: tuple[str, int] | None = None,
    ) -> None:
        """Persist a prepared snapshot and its indexes under the caller's transaction."""
        now = utc_now()
        graph_nodes, graph_edges = project_graph(clean)
        locked = self._lock_source(conn, tenant, connection_id)
        from knowledge_layer.source_permissions import digest

        if source_refresh:
            self._require_source_approval(tenant, locked["kind"], locked["config"])
        clean["source_permission_identity_digest"] = digest(source_identity(locked["kind"], locked["config"]))
        if review_parent is not None:
            if source_refresh:
                raise KnowledgeValidationError("Native discovery cannot inherit reviewed permission lineage")
            readable = conn.execute(select(self.connections.c.id).where(
                self.connections.c.tenant_id == tenant, self.connections.c.id == connection_id,
                self._access_clause(tenant, self.connections.c.id),
            )).first()
            if readable is None:
                raise KnowledgeAccessDenied("Current metadata permission is required for semantic review")
            parent = conn.execute(select(self.snapshots).where(
                self.snapshots.c.tenant_id == tenant, self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).mappings().first()
            if parent is None or (parent["snapshot_id"], parent["version"]) != review_parent:
                raise KnowledgeConflict("Source snapshot changed before semantic review")
            native = parent["payload"]
            from knowledge_layer.source_permissions import SNAPSHOT_MANIFEST_VERSION

            if native.get("source_permission_manifest_version") != SNAPSHOT_MANIFEST_VERSION:
                if self._source_permissions_required:
                    raise KnowledgeAccessDenied("Current native resource permission lineage is unavailable")
                # Existing demo/v1 snapshots remain reviewable. They do not gain
                # a qualified native lineage through a semantic-only review;
                # v2 enforcement still denies their complete source history.
                for control in ("source_permission_manifest_version", "source_permission_manifest_digest",
                                "source_permission_identity_digest"):
                    clean.pop(control, None)
            elif native.get("source_permission_identity_digest") != clean["source_permission_identity_digest"]:
                raise KnowledgeAccessDenied("Current native resource permission lineage is unavailable")
            else:
                clean["source_permission_manifest_digest"] = native["source_permission_manifest_digest"]
        from knowledge_layer.refresh_fence import assert_refresh_fence

        assert_refresh_fence(self, conn, tenant, connection_id)
        version = conn.execute(
            select(self.snapshots.c.version).where(
                self.snapshots.c.tenant_id == tenant,
                self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)
        ).scalar_one_or_none()
        next_version = int(version or 0) + 1
        row_id = uuid4().hex
        conn.execute(
            insert(self.snapshots).values(
                row_id=row_id,
                tenant_id=tenant,
                connection_id=connection_id,
                version=next_version,
                snapshot_id=snapshot_id,
                status=status,
                created_at=now,
                payload=clean,
            )
        )
        self._replace_concept_search(conn, tenant, connection_id, next_version, clean)
        if graph_nodes:
            conn.execute(insert(self.nodes), [
                {"row_id": uuid4().hex, "tenant_id": tenant, "connection_id": connection_id,
                 "version": next_version, "node_id": node.pop("id"), **node}
                for node in graph_nodes
            ])
        if graph_edges:
            conn.execute(insert(self.edges), [
                {"row_id": uuid4().hex, "tenant_id": tenant, "connection_id": connection_id,
                 "version": next_version, "edge_id": edge.pop("id"), **edge}
                for edge in graph_edges
            ])
        connection_values: dict[str, Any] = {"updated_at": now}
        # Reviewing or importing stored metadata is not a new source read.
        # In particular, a review cannot clear a failed connector's state.
        if source_refresh or locked["status"] != "error":
            connection_values.update(status=status, last_error=None if status != "error"
                                     else "Curation returned an error status.")
        if source_refresh and status != "error":
            connection_values["last_refreshed_at"] = now
            if clean.get("discovery_complete") is True:
                connection_values["last_complete_refresh_at"] = now
        conn.execute(
            update(self.connections)
            .where(self.connections.c.tenant_id == tenant, self.connections.c.id == connection_id)
            .values(**connection_values)
        )
        principal = current_principal()
        self._audit(conn, tenant, connection_id, "source.snapshot",
                    principal.subject if principal else "service", {
                        "snapshot_id": snapshot_id, "version": next_version, "status": status,
                        "payload_digest": hashlib.sha256(json.dumps(
                            clean, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, default=str,
                        ).encode()).hexdigest(),
                    })
        from knowledge_layer.refresh_fence import record_refresh_snapshot

        record_refresh_snapshot(self, conn, tenant, connection_id, snapshot_id)

    @staticmethod
    def _has_stale_approved(snapshot: dict[str, Any]) -> bool:
        return any(
            isinstance(item, dict) and (
                item.get("status") == "stale_review"
                or (item.get("status") == "approved" and item.get("effective_on_current_snapshot") is False)
            )
            for item in snapshot.get("proposals", [])
        )

    def publication_preview(self, tenant_id: str, connection_id: str) -> dict[str, Any]:
        """Explain the narrow, evidence-backed gate before steward publication."""
        tenant = validate_tenant_id(tenant_id)
        connection = self.get_connection(tenant, connection_id)
        if connection is None:
            raise KnowledgeNotFound("Connection was not found")
        snapshot = connection.get("snapshot") or {}
        blockers: list[str] = []
        if not snapshot or snapshot.get("discovery_complete") is not True:
            blockers.append("discovery_incomplete")
        if snapshot.get("curation_complete") is False or snapshot.get("curation_errors"):
            blockers.append("curation_incomplete")
        if connection["status"] != "discovery_complete":
            blockers.append("source_not_current")
        if not snapshot.get("assets"):
            blockers.append("no_assets")
        if not snapshot.get("evidence"):
            blockers.append("no_evidence")
        if snapshot.get("errors"):
            blockers.append("discovery_errors")
        if self._has_stale_approved(snapshot):
            blockers.append("stale_approved_assertions")
        if snapshot.get("review_integrity_failed"):
            blockers.append("review_integrity_failed")
        if snapshot.get("dependency_unavailable"):
            blockers.append("dependency_unavailable")
        if snapshot.get("publication") and snapshot["publication"].get("active") is False:
            blockers.append("publication_suspended")
        with self.engine.connect() as conn:
            active = conn.execute(select(self.refresh_jobs.c.id).where(
                self.refresh_jobs.c.tenant_id == tenant,
                self.refresh_jobs.c.connection_id == connection_id,
                self.refresh_jobs.c.status.in_(("queued", "running", "retry_wait")),
            ).limit(1)).first()
            approved_ids = set(conn.execute(select(self.decisions.c.proposal_id).where(
                self.decisions.c.tenant_id == tenant,
                self.decisions.c.connection_id == connection_id,
                self.decisions.c.decision == "approve",
            )).scalars())
        if active:
            blockers.append("refresh_active")
        reviewed_count = sum(
            1 for item in snapshot.get("proposals", [])
            if isinstance(item, dict) and _record_id(item) in approved_ids
            and item.get("status") == "approved"
            and item.get("effective_on_current_snapshot") is True
        )
        if not reviewed_count:
            blockers.append("no_current_owner_review")
        return {
            "connection_id": connection_id, "snapshot_id": snapshot.get("id"),
            "snapshot_version": snapshot.get("version"),
            "eligible": not blockers, "blockers": blockers,
            "reviewed_assertions": reviewed_count,
            "pending_proposals": sum(1 for item in snapshot.get("proposals", [])
                                     if isinstance(item, dict) and item.get("status") in
                                     {"candidate", "pending", "pending_review", "needs_review"}),
            "publication": snapshot.get("publication"),
        }

    def _publication_actor(self) -> str:
        principal = self._require_source_manager()
        if principal is not None and self.source_admin_group not in principal.groups:
            raise KnowledgeAccessDenied("Publication requires the steward group")
        return principal.subject if principal else "operator"

    def publish_source(
        self, tenant_id: str, connection_id: str, *, expected_snapshot_id: str,
        expected_snapshot_version: int, rationale: str,
    ) -> dict[str, Any]:
        tenant = validate_tenant_id(tenant_id)
        actor = self._publication_actor()
        self._require_access(tenant, connection_id, manage=True)
        if not isinstance(rationale, str) or not 16 <= len(rationale.strip()) <= 1000:
            raise KnowledgeValidationError("Publication rationale must be 16-1000 characters")
        # A review recorded during the preview/commit window must not inherit
        # this publication. Readiness compares the latest decision to this time.
        now = utc_now()
        preview = self.publication_preview(tenant, connection_id)
        if (preview["snapshot_id"] != expected_snapshot_id
                or preview["snapshot_version"] != expected_snapshot_version):
            raise KnowledgeConflict("Source snapshot changed before publication")
        if not preview["eligible"]:
            raise KnowledgeConflict("Publication blocked: " + ", ".join(preview["blockers"]))
        with self.engine.begin() as conn:
            record, _created = self._publish_source_in_transaction(
                conn, tenant, connection_id, expected_snapshot_id=expected_snapshot_id,
                expected_snapshot_version=expected_snapshot_version, rationale=rationale,
                actor=actor, published_at=now,
            )
        return record

    def _publish_source_in_transaction(
        self, conn: Any, tenant: str, connection_id: str, *, expected_snapshot_id: str,
        expected_snapshot_version: int, rationale: str, actor: str, published_at: str,
        delegation: dict | None = None,
    ) -> tuple[dict, bool]:
        """Internal commit shared by steward and separately validated policy gates."""
        source = self._lock_source(conn, tenant, connection_id)
        latest = conn.execute(select(self.snapshots.c.snapshot_id, self.snapshots.c.version).where(
            self.snapshots.c.tenant_id == tenant,
            self.snapshots.c.connection_id == connection_id,
        ).order_by(self.snapshots.c.version.desc()).limit(1)).first()
        if (source["status"] != "discovery_complete" or latest is None
                or tuple(latest) != (expected_snapshot_id, expected_snapshot_version)):
            raise KnowledgeConflict("Source snapshot changed before publication")
        current_preview = self.publication_preview(tenant, connection_id)
        if (current_preview["snapshot_id"] != expected_snapshot_id
                or current_preview["snapshot_version"] != expected_snapshot_version):
            raise KnowledgeConflict("Source snapshot changed before publication")
        if not current_preview["eligible"]:
            raise KnowledgeConflict("Publication blocked: " + ", ".join(current_preview["blockers"]))
        existing = conn.execute(select(self.publications).where(
            self.publications.c.tenant_id == tenant,
            self.publications.c.connection_id == connection_id,
            self.publications.c.snapshot_version == expected_snapshot_version,
        )).mappings().first()
        if existing:
            if existing["revoked_at"] is not None:
                raise KnowledgeConflict("This snapshot was withdrawn; refresh before publishing again")
            return dict(existing), False
        conn.execute(insert(self.publications).values(
            tenant_id=tenant, connection_id=connection_id,
            snapshot_id=expected_snapshot_id, snapshot_version=expected_snapshot_version,
            published_by=actor, published_at=published_at, rationale=rationale.strip(),
            revoked_by=None, revoked_at=None, revoke_reason=None,
        ))
        self._audit(conn, tenant, connection_id, "source.publish", actor, {
            "snapshot_id": expected_snapshot_id, "snapshot_version": expected_snapshot_version,
            "rationale_digest": hashlib.sha256(rationale.strip().encode()).hexdigest(),
            **({"delegation": delegation} if delegation else {}),
        })
        return {
            "snapshot_id": expected_snapshot_id, "snapshot_version": expected_snapshot_version,
            "published_by": actor, "published_at": published_at, "rationale": rationale.strip(),
        }, True

    def revoke_publication(self, tenant_id: str, connection_id: str, *, reason: str) -> dict[str, Any]:
        tenant = validate_tenant_id(tenant_id)
        actor = self._publication_actor()
        self._require_access(tenant, connection_id, manage=True)
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 1000:
            raise KnowledgeValidationError("Withdrawal reason must be 8-1000 characters")
        snapshot = self.get_snapshot(tenant, connection_id)
        if not snapshot:
            raise KnowledgeNotFound("Current source publication was not found")
        with self.engine.begin() as conn:
            self._lock_source(conn, tenant, connection_id)
            latest_version = conn.execute(select(self.snapshots.c.version).where(
                self.snapshots.c.tenant_id == tenant,
                self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).scalar_one_or_none()
            if latest_version != snapshot["version"]:
                raise KnowledgeConflict("Source snapshot changed before withdrawal")
            existing = conn.execute(select(self.publications.c.snapshot_id).where(
                self.publications.c.tenant_id == tenant,
                self.publications.c.connection_id == connection_id,
                self.publications.c.snapshot_version == snapshot["version"],
                self.publications.c.revoked_at.is_(None),
            )).scalar_one_or_none()
            if existing != snapshot["id"]:
                raise KnowledgeNotFound("Current source publication was not found")
            result = conn.execute(update(self.publications).where(
                self.publications.c.tenant_id == tenant,
                self.publications.c.connection_id == connection_id,
                self.publications.c.snapshot_version == snapshot["version"],
                self.publications.c.revoked_at.is_(None),
            ).values(revoked_by=actor, revoked_at=utc_now(), revoke_reason=reason.strip()))
            if result.rowcount != 1:
                raise KnowledgeConflict("Publication changed before withdrawal")
            self._audit(conn, tenant, connection_id, "source.withdraw_publication", actor, {
                "snapshot_id": snapshot["id"], "snapshot_version": snapshot["version"],
                "reason_digest": hashlib.sha256(reason.strip().encode()).hexdigest(),
            })
        return {"connection_id": connection_id, "snapshot_id": snapshot["id"], "agent_ready": False}

    def get_graph(
        self, tenant_id: str, connection_id: str, *, focus_id: str | None = None,
        depth: int = 1, limit: int = 60, version: int | None = None,
    ) -> dict[str, Any]:
        """Return a bounded neighborhood from the versioned graph index."""
        tenant = validate_tenant_id(tenant_id)
        self._require_access(tenant, connection_id)
        if not 1 <= limit <= 100 or not 0 <= depth <= 2:
            raise KnowledgeValidationError("Graph limit must be 1-100 and depth must be 0-2")
        if focus_id is not None and (not focus_id or len(focus_id) > 160):
            raise KnowledgeValidationError("focus_id must be 1-160 characters")
        with self.engine.connect() as conn:
            selected = select(self.snapshots.c.version, self.snapshots.c.snapshot_id, self.snapshots.c.payload).where(
                self.snapshots.c.tenant_id == tenant,
                self.snapshots.c.connection_id == connection_id,
            )
            if version is None:
                selected = selected.order_by(self.snapshots.c.version.desc()).limit(1)
            else:
                selected = selected.where(self.snapshots.c.version == version).limit(1)
            snapshot_row = conn.execute(selected).first()
            if snapshot_row is None:
                raise KnowledgeNotFound("Source snapshot was not found")
            record_source_read(tenant, connection_id)
            graph_version, snapshot_id, snapshot_payload = snapshot_row
            # Reviewed meaning and cross-source document citations are applied
            # at read time, so the persisted source graph can be out of date.
            has_decisions = conn.execute(
                select(self.decisions.c.proposal_id).where(
                    self.decisions.c.tenant_id == tenant,
                    self.decisions.c.connection_id == connection_id,
                ).limit(1)
            ).first() is not None
            if has_decisions:
                current = self.get_snapshot(tenant, connection_id, version=graph_version) or snapshot_payload
                try:
                    current_nodes, current_edges = slice_legacy_graph(
                        current, focus_id=focus_id, depth=depth, limit=limit,
                    )
                except KeyError as exc:
                    raise KnowledgeNotFound("Graph node was not found") from exc
                return {
                    "connection_id": connection_id, "snapshot_id": snapshot_id,
                    "version": graph_version, "focus_id": focus_id,
                    "nodes": current_nodes, "edges": current_edges,
                    "truncated": len(current_nodes) >= limit or len(current_edges) >= limit * 2,
                }
            scope = (
                self.nodes.c.tenant_id == tenant,
                self.nodes.c.connection_id == connection_id,
                self.nodes.c.version == graph_version,
            )
            edge_scope = (
                self.edges.c.tenant_id == tenant,
                self.edges.c.connection_id == connection_id,
                self.edges.c.version == graph_version,
            )
            indexed = conn.execute(select(self.nodes.c.node_id).where(*scope).limit(1)).first()
            if indexed is None:
                try:
                    old_nodes, old_edges = slice_legacy_graph(snapshot_payload, focus_id=focus_id, depth=depth, limit=limit)
                except KeyError as exc:
                    raise KnowledgeNotFound("Graph node was not found") from exc
                return {
                    "connection_id": connection_id, "snapshot_id": snapshot_id,
                    "version": graph_version, "focus_id": focus_id,
                    "nodes": old_nodes, "edges": old_edges,
                    "truncated": len(old_nodes) >= limit or len(old_edges) >= limit * 2,
                }
            if focus_id:
                seed = conn.execute(select(self.nodes).where(*scope, self.nodes.c.node_id == focus_id)).mappings().first()
                if seed is None:
                    raise KnowledgeNotFound("Graph node was not found")
                selected_nodes = {focus_id: dict(seed)}
                frontier = {focus_id}
            else:
                seeds = conn.execute(
                    select(self.nodes).where(*scope)
                    .order_by(self.nodes.c.kind, self.nodes.c.label).limit(min(limit, 12))
                ).mappings().all()
                selected_nodes = {row["node_id"]: dict(row) for row in seeds}
                frontier = set(selected_nodes)
            selected_edges: dict[str, dict[str, Any]] = {}
            for _ in range(depth):
                if not frontier or len(selected_nodes) >= limit:
                    break
                candidates = conn.execute(
                    select(self.edges).where(
                        *edge_scope,
                        or_(self.edges.c.subject_id.in_(frontier), self.edges.c.object_id.in_(frontier)),
                    ).order_by(self.edges.c.predicate, self.edges.c.edge_id).limit(limit * 4)
                ).mappings().all()
                neighbors = []
                for edge in candidates:
                    other = edge["object_id"] if edge["subject_id"] in frontier else edge["subject_id"]
                    if other not in selected_nodes and other not in neighbors and len(selected_nodes) + len(neighbors) < limit:
                        neighbors.append(other)
                if neighbors:
                    for row in conn.execute(select(self.nodes).where(*scope, self.nodes.c.node_id.in_(neighbors))).mappings():
                        selected_nodes[row["node_id"]] = dict(row)
                frontier = set(neighbors)
                for edge in candidates:
                    if edge["subject_id"] in selected_nodes and edge["object_id"] in selected_nodes:
                        selected_edges[edge["edge_id"]] = dict(edge)
            # Include links among overview seeds even when the node budget is full.
            if selected_nodes and not selected_edges:
                ids = set(selected_nodes)
                for edge in conn.execute(
                    select(self.edges).where(*edge_scope, self.edges.c.subject_id.in_(ids), self.edges.c.object_id.in_(ids))
                    .limit(limit * 2)
                ).mappings():
                    selected_edges[edge["edge_id"]] = dict(edge)
        return {
            "connection_id": connection_id,
            "snapshot_id": snapshot_id,
            "version": graph_version,
            "focus_id": focus_id,
            "nodes": [self._graph_node(row) for row in selected_nodes.values()],
            "edges": [self._graph_edge(row) for row in list(selected_edges.values())[:limit * 2]],
            "truncated": len(selected_nodes) >= limit or len(selected_edges) >= limit * 2,
        }

    @staticmethod
    def _graph_node(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["node_id"], "kind": row["kind"], "label": row["label"],
                "description": row["description"], "status": row["status"],
                "trust_state": row["trust_state"], "evidence_ids": row["evidence_ids"]}

    @staticmethod
    def _graph_edge(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row["edge_id"], "subject_id": row["subject_id"],
                "predicate": row["predicate"], "object_id": row["object_id"],
                "status": row["status"], "trust_state": row["trust_state"],
                "evidence_ids": row["evidence_ids"],
                "confidence": float(row["confidence"]) if row["confidence"] is not None else None}

    def refresh_connection(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        curator: Callable[..., dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tenant = validate_tenant_id(tenant_id)
        self._require_source_manager()
        self._require_access(tenant, connection_id, manage=True)
        connection = self._management_connection(tenant, connection_id)
        self._require_source_approval(tenant, connection["kind"], connection["config"])
        if connection["kind"] == "external_adapter" and connection["config"].get("discovery_mode") == "inventory":
            if curator is not None:
                raise KnowledgeValidationError("Inventory discovery cannot run a legacy curator")
            from knowledge_layer.inventory_refresh import refresh_inventory

            return refresh_inventory(self, tenant, connection_id)
        if curator is None:
            try:
                if self.source_policy is not None or self.access_enforced:
                    from knowledge_layer.connector_isolation import isolated_curator

                    curator = isolated_curator
                else:
                    from knowledge_layer.curation import curate_source

                    curator = curate_source
            except ImportError as exc:
                raise CurationFailed("Source curation is unavailable") from exc
        curation_config = dict(connection["config"])
        previous_snapshot = connection.get("snapshot")
        if connection["kind"] == "local_files":
            curation_config["root"] = str(self.local_files_root)
        curation_config["source_id"] = "source_" + hashlib.sha256(f"{tenant}:{connection_id}".encode()).hexdigest()[:24]
        curation_config["previous_snapshot"] = previous_snapshot
        with self.engine.begin() as conn:
            locked = self._lock_source(conn, tenant, connection_id)
            self._require_source_approval(tenant, locked["kind"], locked["config"])
            from knowledge_layer.refresh_fence import assert_refresh_fence

            assert_refresh_fence(self, conn, tenant, connection_id)
            conn.execute(update(self.connections).where(
                self.connections.c.tenant_id == tenant, self.connections.c.id == connection_id,
            ).values(last_refresh_attempt_at=utc_now()))
            assert_refresh_fence(self, conn, tenant, connection_id)
        try:
            payload = curator(connection["kind"], curation_config)
            if not isinstance(payload, dict):
                raise KnowledgeValidationError("curation adapter returned a non-object snapshot")
            payload_complete = payload.get("discovery_complete") is True or (
                payload.get("discovery_complete") is None
                and payload.get("status") == "ready"
            )
            if (
                previous_snapshot
                and previous_snapshot.get("discovery_complete") is True
                and (not payload_complete or payload.get("status") == "error")
            ):
                # A failed or incomplete scan cannot prove that previously
                # observed assets disappeared. Keep the last complete version
                # queryable and expose the connection failure separately.
                self._mark_error(tenant, connection_id)
                return self._refresh_response(tenant, connection_id, failed=True)
            snapshot = self.save_snapshot(tenant, connection_id, payload, source_refresh=True)
            result = self._refresh_response(tenant, connection_id)
            if result["snapshot"] is not None:
                result["snapshot"] = snapshot
            return result
        except Exception as exc:
            self._mark_error(tenant, connection_id)
            if isinstance(exc, KnowledgeError):
                raise
            raise CurationFailed("Source curation failed; adapter details are not stored") from exc

    def get_concepts(self, tenant_id: str, connection_id: str | None = None) -> list[dict[str, Any]]:
        tenant = validate_tenant_id(tenant_id)
        if connection_id is not None:
            connection = self.get_connection(tenant, connection_id)
            if connection is None:
                raise KnowledgeNotFound("Connection was not found")
            snapshot = connection.get("snapshot")
            snapshots = [(connection, snapshot)] if snapshot else []
        else:
            snapshots = (
                (connection, connection["snapshot"])
                for connection in self.iter_connections(tenant)
                if connection.get("snapshot")
            )
        concepts = []
        seen: set[tuple[str, str]] = set()
        for connection, snapshot in snapshots:
            for concept in snapshot.get("concepts", []):
                if not isinstance(concept, dict):
                    continue
                concept_id = _record_id(concept)
                if not concept_id:
                    continue
                key = (str(connection["id"]), concept_id)
                if key in seen:
                    continue
                seen.add(key)
                concepts.append({**concept, "id": concept_id, "connection_id": connection["id"], "connection_name": connection["name"]})
        return concepts

    def _context_connections(self, tenant_id: str, connection_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fresh grant-filtered projections for one bounded context/search batch."""
        tenant = validate_tenant_id(tenant_id)
        ids = list(dict.fromkeys(connection_ids))
        if len(ids) > 20:
            raise KnowledgeValidationError("Context source batch exceeds 20 connections")
        if not ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.connections).where(
                self.connections.c.tenant_id == tenant,
                self.connections.c.id.in_(ids),
                self._access_clause(tenant, self.connections.c.id),
            )).mappings().all()
        return {row["id"]: row for row in self._connection_responses_for_rows(tenant, rows)}

    def _context_cross_source_concepts(
        self, tenant_id: str, concepts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Read only current label/type hints; selected suggestions are reprojected."""
        tenant = validate_tenant_id(tenant_id)
        targets: dict[str, list[Any]] = {}
        for concept in concepts:
            label = normalize_name(str(concept.get("name") or ""))
            if label:
                targets.setdefault(label, []).append(concept.get("semantic_type"))
        if not targets:
            return []
        snapshots = self.snapshots.c
        ranked = select(
            snapshots.connection_id, snapshots.payload,
            func.row_number().over(
                partition_by=snapshots.connection_id, order_by=snapshots.version.desc(),
            ).label("source_rank"),
        ).where(snapshots.tenant_id == tenant).subquery()
        statement = select(
            self.connections.c.id, self.connections.c.name,
            ranked.c.payload["concepts"].label("concepts"),
        ).join(ranked, and_(
            ranked.c.connection_id == self.connections.c.id, ranked.c.source_rank == 1,
        )).where(
            self.connections.c.tenant_id == tenant,
            self._access_clause(tenant, self.connections.c.id),
        ).order_by(self.connections.c.created_at.desc(), self.connections.c.id.desc())
        result = []
        with self.engine.connect().execution_options(yield_per=20) as conn:
            for row in conn.execute(statement).mappings():
                record_source_read(tenant, row["id"])
                seen: set[str] = set()
                for item in row["concepts"] or []:
                    if not isinstance(item, dict) or not (identifier := _record_id(item)) or identifier in seen:
                        continue
                    seen.add(identifier)
                    label = normalize_name(str(item.get("name") or ""))
                    if label not in targets or item.get("semantic_type") not in targets[label]:
                        continue
                    result.append({"id": identifier, "name": item.get("name"),
                                   "semantic_type": item.get("semantic_type"),
                                   "connection_id": row["id"], "connection_name": row["name"]})
        return result

    def get_concept(self, tenant_id: str, concept_id: str) -> dict[str, Any] | None:
        tenant = validate_tenant_id(tenant_id)
        if concept_id.startswith("inventory_concept_"):
            from knowledge_layer.inventory_concepts import InventoryConcepts

            context = InventoryConcepts(self).find(tenant, concept_id)
            return context["concept"] if context else None
        for connection in self.iter_connections(tenant):
            snapshot = connection.get("snapshot")
            if not snapshot:
                continue
            for concept in snapshot.get("concepts", []):
                if isinstance(concept, dict) and _record_id(concept) == concept_id:
                    return {**concept, "id": concept_id, "connection_id": connection["id"], "connection_name": connection["name"]}
        return None

    def get_context(
        self,
        tenant_id: str,
        concept_id: str | None = None,
        term: str | None = None,
        connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        tenant = validate_tenant_id(tenant_id)
        if bool(concept_id) == bool(term):
            raise KnowledgeValidationError("provide exactly one of concept_id or term")
        source = self.get_connection(tenant, connection_id) if connection_id else None
        if connection_id and source is None:
            return None
        if concept_id and concept_id.startswith("inventory_concept_"):
            from knowledge_layer.inventory_concepts import InventoryConcepts

            return InventoryConcepts(self).find(tenant, concept_id, connection_id=connection_id)
        if term and source is not None:
            seen: set[str] = set()
            all_concepts = []
            for item in (source.get("snapshot") or {}).get("concepts", []):
                if not isinstance(item, dict) or not (identifier := _record_id(item)) or identifier in seen:
                    continue
                seen.add(identifier)
                all_concepts.append({**item, "id": identifier, "connection_id": source["id"],
                                     "connection_name": source["name"]})
        else:
            all_concepts = self.get_concepts(tenant)
        if term:
            from knowledge_layer.retrieval import term_context

            return term_context(self, tenant_id=tenant, term=term, connection_id=connection_id,
                                concepts=all_concepts, connection=source)
        if source is not None:
            concept = next((
                {**item, "id": str(concept_id), "connection_id": source["id"], "connection_name": source["name"]}
                for item in (source.get("snapshot") or {}).get("concepts", [])
                if isinstance(item, dict) and _record_id(item) == concept_id
            ), None)
        else:
            concept = self.get_concept(tenant, str(concept_id))
        if concept is None or (connection_id and concept.get("connection_id") != connection_id):
            return None
        connection = self.get_connection(tenant, concept["connection_id"])
        if connection is None:
            raise KnowledgeConflict("Source context changed during assembly; reload current meaning")
        context = self._context_for_concept(tenant, concept, all_concepts, connection)
        from knowledge_layer.retrieval import _recheck_context_sources

        _recheck_context_sources(self, tenant, {connection["id"]: connection},
                                 {"matches": [context], "retrieval_candidates": []})
        return context

    def _validate_proposal_approval(
        self, tenant: str, proposal: dict[str, Any], snapshot: dict[str, Any],
    ) -> None:
        """Apply the same evidence and dependency rules to individual and batch review."""
        evidence_ids = _evidence_ids(proposal)
        available = {_record_id(item) for item in (snapshot or {}).get("evidence", []) if isinstance(item, dict)}
        if not evidence_ids or not set(evidence_ids).issubset(available):
            raise KnowledgeValidationError("A proposal needs evidence handles from this snapshot before approval")
        if proposal.get("proposal_type") == "semantic_correction":
            current_concepts = {_record_id(item) for item in (snapshot or {}).get("concepts", [])
                                if isinstance(item, dict)}
            if proposal.get("target_concept_id") not in current_concepts:
                raise KnowledgeConflict("The semantic target changed before approval; fetch fresh evidence")
            mapping = proposal.get("proposed_mapping")
            if mapping is not None and (
                not isinstance(mapping, dict)
                or not _semantic_mapping_current(snapshot or {}, mapping, evidence_ids)
            ):
                raise KnowledgeConflict("The semantic relationship changed before approval; fetch fresh evidence")
        if proposal.get("proposal_type") == "contract_assertion" and not self._contract_dependencies_current(
            tenant, proposal
        ):
            raise KnowledgeConflict("The cited document source changed before approval")
        if proposal.get("assertion_kind") == "cross_source_join":
            from knowledge_layer.cross_source import (
                source_binding_current,
                target_binding_current,
            )

            if (not source_binding_current(snapshot or {}, proposal.get("payload") or {})
                    or not target_binding_current(self, tenant, proposal.get("payload") or {})):
                raise KnowledgeConflict("The relationship source binding changed before approval")

    def decide_proposal(
        self,
        tenant_id: str,
        connection_id: str,
        proposal_id: str,
        *,
        decision: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        if proposal_id.startswith("inventory_concept_"):
            from knowledge_layer.inventory_concepts import InventoryConcepts

            return InventoryConcepts(self).decide(tenant_id, connection_id, concept_id=proposal_id,
                                                  decision=decision, rationale=rationale)
        tenant = validate_tenant_id(tenant_id)
        self._require_source_manager()
        self._require_access(tenant, connection_id, manage=True)
        if decision not in {"approve", "reject"}:
            raise KnowledgeValidationError("decision must be 'approve' or 'reject'")
        if rationale is not None:
            rationale = rationale.strip()
            if len(rationale) > 1000:
                raise KnowledgeValidationError("rationale must be at most 1000 characters")
        connection = self.get_connection(tenant, connection_id)
        if connection is None:
            raise KnowledgeNotFound("Connection was not found")
        snapshot = connection.get("snapshot")
        proposal = next((item for item in (snapshot or {}).get("proposals", []) if isinstance(item, dict) and _record_id(item) == proposal_id), None)
        local_proposal = None
        if proposal is None:
            with self.engine.connect() as conn:
                row = conn.execute(
                    select(self.proposals).where(
                        self.proposals.c.tenant_id == tenant,
                        self.proposals.c.connection_id == connection_id,
                        self.proposals.c.id == proposal_id,
                        self.proposals.c.archived_at.is_(None),
                    )
                ).mappings().first()
                if row is not None:
                    local_proposal = dict(row)
                    proposal = _semantic_proposal_response(local_proposal)
                else:
                    contract_row = conn.execute(
                        select(self.contract_proposals).where(
                            self.contract_proposals.c.tenant_id == tenant,
                            self.contract_proposals.c.connection_id == connection_id,
                            self.contract_proposals.c.id == proposal_id,
                            self.contract_proposals.c.archived_at.is_(None),
                        )
                    ).mappings().first()
                    if contract_row is not None:
                        local_proposal = dict(contract_row)
                        proposal = _contract_proposal_response(local_proposal)
        if proposal is None:
            raise KnowledgeNotFound("Proposal was not found")
        if decision == "approve":
            self._validate_proposal_approval(tenant, proposal, snapshot or {})
        decided_at = utc_now()
        digest = _proposal_digest(proposal, (snapshot or {}).get("evidence", []))
        values = {"tenant_id": tenant, "connection_id": connection_id, "proposal_id": proposal_id,
                  "decision": decision, "rationale": rationale, "decided_at": decided_at,
                  "proposal_digest": digest}
        reviewer = current_principal()
        new_decision = False
        with self.engine.begin() as conn:
            self._lock_source(conn, tenant, connection_id)
            latest = conn.execute(select(self.snapshots.c.snapshot_id, self.snapshots.c.version).where(
                self.snapshots.c.tenant_id == tenant,
                self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).first()
            if latest is None or tuple(latest) != (snapshot["id"], snapshot["version"]):
                raise KnowledgeConflict("Source snapshot changed before review")
            if (decision == "approve" and proposal.get("proposal_type") == "contract_assertion"
                    and not self._contract_dependencies_current(tenant, proposal)):
                raise KnowledgeConflict("The cited source changed before approval")
            existing = conn.execute(
                select(self.decisions).where(
                    self.decisions.c.tenant_id == tenant,
                    self.decisions.c.connection_id == connection_id,
                    self.decisions.c.proposal_id == proposal_id,
                )
            ).mappings().first()
            if existing:
                if existing["decision"] != decision:
                    raise KnowledgeConflict("Proposal already has a different recorded decision")
                if existing["proposal_digest"] != digest:
                    conn.execute(update(self.decisions).where(
                        self.decisions.c.tenant_id == tenant,
                        self.decisions.c.connection_id == connection_id,
                        self.decisions.c.proposal_id == proposal_id,
                    ).values(rationale=rationale, decided_at=decided_at, proposal_digest=digest))
                    new_decision = True
                else:
                    values = dict(existing)
            else:
                conn.execute(insert(self.decisions).values(**values))
                new_decision = True
            if new_decision:
                self._audit(conn, tenant, connection_id, "proposal.decide",
                            reviewer.subject if reviewer else "operator", {
                                "proposal_id": proposal_id, "decision": decision,
                                "proposal_digest": digest,
                                "rationale_digest": hashlib.sha256((rationale or "").encode()).hexdigest(),
                                "re_review": existing is not None,
                            })
        refreshed = self.get_snapshot(tenant, connection_id) or {}
        if new_decision and refreshed:
            versioned = {
                field: deepcopy(refreshed[field])
                for field in _ALLOWED_SNAPSHOT_FIELDS
                if field in refreshed
            }
            versioned["snapshot_id"] = f"{refreshed.get('id', 'snapshot')}.review-{uuid4().hex[:8]}"
            from knowledge_layer.contracts import strip_materialized_assertions

            strip_materialized_assertions(versioned)
            refreshed = self._save_review_snapshot(tenant, connection_id, versioned,
                parent=(refreshed["id"], refreshed["version"]))
        updated_proposal = next((item for item in refreshed.get("proposals", []) if _record_id(item) == proposal_id), proposal)
        if local_proposal is not None:
            updated_proposal = _semantic_proposal_response({**local_proposal, "status": "approved" if decision == "approve" else "rejected", "decision": decision, "decision_rationale": rationale, "decided_at": decided_at})
        relationship = next((item for item in refreshed.get("relationships", []) if item.get("decision_proposal_id") == proposal_id), None)
        return {"proposal": updated_proposal, "relationship": relationship, "decision": values}

    def submit_proposal(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        target_concept_id: str,
        proposed_definition: str | None = None,
        proposed_mapping: dict[str, Any] | None = None,
        evidence_ids: list[str],
        rationale: str,
        proposal_type: str = "semantic_correction",
        expected_snapshot_id: str | None = None,
        _transaction: Any = None,
    ) -> dict[str, Any]:
        """Record a pending, evidence-bound semantic correction for review."""
        tenant = validate_tenant_id(tenant_id)
        connection = self.get_connection(tenant, connection_id)
        if connection is None:
            raise KnowledgeNotFound("Connection was not found")
        if not isinstance(target_concept_id, str) or not target_concept_id.strip() or len(target_concept_id) > 160:
            raise KnowledgeValidationError("target_concept_id must be 1-160 characters")
        if proposal_type != "semantic_correction":
            raise KnowledgeValidationError("proposal_type must be semantic_correction")
        if proposed_definition is not None and (not isinstance(proposed_definition, str) or len(proposed_definition.strip()) > 4000):
            raise KnowledgeValidationError("proposed_definition must be at most 4000 characters")
        if proposed_mapping is not None and not isinstance(proposed_mapping, dict):
            raise KnowledgeValidationError("proposed_mapping must be an object")
        if not proposed_definition and not proposed_mapping:
            raise KnowledgeValidationError("Provide a proposed definition or mapping")
        rationale = rationale.strip() if isinstance(rationale, str) else ""
        if not 8 <= len(rationale) <= 1000:
            raise KnowledgeValidationError("rationale must be 8-1000 characters")
        if not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 20:
            raise KnowledgeValidationError("evidence_ids must contain 1-20 evidence handles")
        evidence_ids = list(dict.fromkeys(str(item).strip() for item in evidence_ids if str(item).strip()))
        snapshot = connection.get("snapshot") or {}
        if expected_snapshot_id is not None and expected_snapshot_id != snapshot.get("id"):
            raise KnowledgeConflict("Source changed after the curation work packet was issued; fetch fresh evidence")
        concepts = {_record_id(item) for item in snapshot.get("concepts", []) if isinstance(item, dict)}
        evidence = {_record_id(item) for item in snapshot.get("evidence", []) if isinstance(item, dict)}
        if target_concept_id not in concepts:
            raise KnowledgeValidationError("target concept must exist in the current source snapshot")
        if not set(evidence_ids).issubset(evidence):
            raise KnowledgeValidationError("evidence_ids must refer to evidence in the current source snapshot")
        clean_mapping = _sanitize(proposed_mapping) if proposed_mapping is not None else None
        if clean_mapping is not None and proposal_type == "semantic_correction":
            expected_keys = {"relationship_id", "subject_id", "predicate", "object_id"}
            if set(clean_mapping) != expected_keys or clean_mapping.get("subject_id") != target_concept_id:
                raise KnowledgeValidationError("proposed_mapping must identify one current relationship for the target concept")
            relationship = next((
                item for item in snapshot.get("relationships", [])
                if isinstance(item, dict) and _record_id(item) == clean_mapping["relationship_id"]
            ), None)
            if relationship is None or any(
                clean_mapping[key] != relationship.get(key)
                for key in ("subject_id", "predicate", "object_id")
            ) or clean_mapping["object_id"] not in concepts:
                raise KnowledgeValidationError("proposed_mapping must match a current relationship between source concepts")
            if not set(evidence_ids).intersection(_evidence_ids(relationship)):
                raise KnowledgeValidationError("proposed_mapping must cite evidence for that relationship")
        if len(json.dumps(clean_mapping, ensure_ascii=False, separators=(",", ":"))) > 10000:
            raise KnowledgeValidationError("proposed_mapping exceeds the 10 KiB limit")
        proposal_id = f"proposal_{uuid4().hex}"
        created_at = utc_now()
        principal = current_principal() if self.access_enforced else None
        actor = principal.subject if principal else "operator"
        row = {
            "id": proposal_id,
            "tenant_id": tenant,
            "connection_id": connection_id,
            "proposal_type": proposal_type,
            "target_concept_id": target_concept_id,
            "proposed_definition": proposed_definition.strip() if proposed_definition else None,
            "proposed_mapping": clean_mapping,
            "evidence_ids": evidence_ids,
            "rationale": rationale,
            "status": "pending_review",
            "created_at": created_at,
            "submitted_by": actor,
        }
        from contextlib import nullcontext

        with (nullcontext(_transaction) if _transaction is not None else self.engine.begin()) as conn:
            self._require_proposal_capacity(conn, tenant, connection_id, actor=actor, steward=bool(
                principal is None or self.source_admin_group in principal.groups
            ))
            latest = conn.execute(select(self.snapshots.c.snapshot_id).where(
                self.snapshots.c.tenant_id == tenant, self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).scalar_one_or_none()
            if latest != snapshot.get("id"):
                raise KnowledgeConflict("Source changed before the proposal was stored")
            conn.execute(insert(self.proposals).values(**row))
            self._audit(conn, tenant, connection_id, "proposal.submit", actor, {
                "proposal_id": proposal_id, "proposal_type": proposal_type,
            })
        return _semantic_proposal_response(row)

    def submit_contract_assertion(
        self, tenant_id: str, connection_id: str, *, assertion_kind: str,
        asset_id: str, payload: dict[str, Any], evidence_ids: list[str],
        rationale: str, expected_snapshot_id: str,
        evidence_refs: list[dict[str, str]] | None = None,
        _transaction: Any = None,
    ) -> dict[str, Any]:
        """Save a typed, evidence-bound semantic contract assertion for review."""
        from knowledge_layer.contracts import validate_assertion

        tenant = validate_tenant_id(tenant_id)
        connection = self.get_connection(tenant, connection_id)
        if connection is None:
            raise KnowledgeNotFound("Connection was not found")
        snapshot = connection.get("snapshot") or {}
        if not expected_snapshot_id or expected_snapshot_id != snapshot.get("id"):
            raise KnowledgeConflict("Source changed after the contract work packet was issued; fetch fresh evidence")
        rationale = rationale.strip() if isinstance(rationale, str) else ""
        if not 8 <= len(rationale) <= 1000:
            raise KnowledgeValidationError("rationale must be 8-1000 characters")
        refs = self._validate_external_evidence_refs(
            tenant, connection_id, evidence_refs or [], allow_structured=assertion_kind == "cross_source_join")
        if assertion_kind == "cross_source_join":
            from knowledge_layer.cross_source import validate_cross_source_join

            clean_payload = validate_cross_source_join(
                self, tenant_id=tenant, connection_id=connection_id, snapshot=snapshot,
                asset_id=asset_id, payload=payload, evidence_ids=evidence_ids, evidence_refs=refs)
        else:
            clean_payload = validate_assertion(
                snapshot, kind=assertion_kind, asset_id=asset_id, payload=payload,
                evidence_ids=evidence_ids, source_kind=connection["kind"],
            )
        if assertion_kind == "term_binding":
            from knowledge_layer.contracts import validate_term_binding_target

            target = self.get_snapshot(tenant, clean_payload["target_connection_id"])
            validate_term_binding_target(target or {}, clean_payload, refs)
        if len(json.dumps(clean_payload, ensure_ascii=False)) > 10000:
            raise KnowledgeValidationError("Contract assertion exceeds the 10 KiB limit")
        principal = current_principal() if self.access_enforced else None
        actor = principal.subject if principal else "operator"
        row = {
            "id": f"proposal_{uuid4().hex}", "tenant_id": tenant, "connection_id": connection_id,
            "source_snapshot_id": expected_snapshot_id, "assertion_kind": assertion_kind,
            "asset_id": asset_id, "payload": clean_payload, "evidence_ids": evidence_ids,
            "evidence_refs": refs,
            "rationale": rationale, "created_at": utc_now(), "submitted_by": actor,
        }
        from contextlib import nullcontext

        with (nullcontext(_transaction) if _transaction is not None else self.engine.begin()) as conn:
            self._require_proposal_capacity(conn, tenant, connection_id, actor=actor, steward=bool(
                principal is None or self.source_admin_group in principal.groups
            ))
            # Capacity acquisition serializes target writers. Revalidate both
            # the target and every citation after that wait, before storing.
            latest = conn.execute(select(self.snapshots.c.snapshot_id).where(
                self.snapshots.c.tenant_id == tenant, self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).scalar_one_or_none()
            if latest != expected_snapshot_id:
                raise KnowledgeConflict("Source changed before the assertion was stored")
            self._validate_external_evidence_refs(tenant, connection_id, refs,
                allow_structured=assertion_kind == "cross_source_join")
            if assertion_kind == "term_binding" and not self._term_binding_target_current(
                tenant, clean_payload, refs, require_review=False,
            ):
                raise KnowledgeConflict("The term binding target changed during drafting")
            conn.execute(insert(self.contract_proposals).values(**row))
            self._audit(conn, tenant, connection_id, "proposal.submit", actor, {
                "proposal_id": row["id"], "proposal_type": "contract_assertion",
            })
        return _contract_proposal_response(row)

    def _require_proposal_capacity(
        self, conn: Any, tenant: str, connection_id: str, *, actor: str, steward: bool,
    ) -> None:
        # Serialize writers for the same source on PostgreSQL. The fixed cap also
        # bounds ordinary snapshot projection without adding a broker or cache.
        self._lock_source(conn, tenant, connection_id, manage=False)
        proposal_tables = (self.proposals, self.contract_proposals, self.inventory_concept_proposals)
        total = sum(conn.execute(select(func.count()).select_from(table).where(
            table.c.tenant_id == tenant, table.c.connection_id == connection_id,
            table.c.archived_at.is_(None),
        )).scalar_one() for table in proposal_tables)
        if total >= MAX_SOURCE_PROPOSALS:
            raise KnowledgeConflict("Source proposal limit reached; ask a steward to archive reviewed proposals")
        if not steward:
            reserve = min(STEWARD_PROPOSAL_RESERVE, max(1, MAX_SOURCE_PROPOSALS // 10))
            if total >= MAX_SOURCE_PROPOSALS - reserve:
                raise KnowledgeConflict("Source proposal limit reserved for steward review")
            owned = sum(conn.execute(select(func.count()).select_from(table).where(
                table.c.tenant_id == tenant,
                table.c.connection_id == connection_id,
                table.c.archived_at.is_(None),
                table.c.submitted_by == actor,
            )).scalar_one() for table in proposal_tables)
            if owned >= MAX_SUBMITTER_PROPOSALS:
                raise KnowledgeConflict("Contributor proposal limit reached; ask a steward to archive reviewed proposals")

    def archive_proposals(
        self, tenant_id: str, connection_id: str, *, proposal_ids: list[str], reason: str,
    ) -> dict[str, Any]:
        """Steward-only logical archive; review and audit rows remain intact."""
        tenant = validate_tenant_id(tenant_id)
        actor = self._publication_actor()
        self._require_access(tenant, connection_id, manage=True)
        if not isinstance(proposal_ids, list) or not 1 <= len(proposal_ids) <= 100:
            raise KnowledgeValidationError("Archive requires 1-100 proposal IDs")
        if any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in proposal_ids):
            raise KnowledgeValidationError("Archive proposal IDs must be 1-128 characters")
        ids = list(dict.fromkeys(proposal_ids))
        reason = reason.strip() if isinstance(reason, str) else ""
        if not 8 <= len(reason) <= 1000:
            raise KnowledgeValidationError("Archive reason must be 8-1000 characters")
        now = utc_now()
        rows_by_table: dict[Any, list[dict[str, Any]]] = {}
        with self.engine.begin() as conn:
            self._lock_source(conn, tenant, connection_id)
            found: set[str] = set()
            for table in (self.proposals, self.contract_proposals, self.inventory_concept_proposals):
                rows = [dict(row) for row in conn.execute(select(table.c.id, table.c.archived_at).where(
                    table.c.tenant_id == tenant,
                    table.c.connection_id == connection_id,
                    table.c.id.in_(ids),
                )).mappings()]
                rows_by_table[table] = rows
                found.update(row["id"] for row in rows)
            if found != set(ids):
                raise KnowledgeNotFound("One or more proposals were not found on this source")
            newly_archived = [row["id"] for rows in rows_by_table.values() for row in rows
                              if row["archived_at"] is None]
            if not newly_archived:
                return {"connection_id": connection_id, "archived_ids": ids,
                        "archived_count": 0, "publication_withdrawn": False}
            decisions = conn.execute(select(self.decisions.c.proposal_id).where(
                self.decisions.c.tenant_id == tenant,
                self.decisions.c.connection_id == connection_id,
                self.decisions.c.proposal_id.in_(newly_archived),
                self.decisions.c.decision == "approve",
            )).scalars().all()
            for table, rows in rows_by_table.items():
                active_ids = [row["id"] for row in rows if row["archived_at"] is None]
                if active_ids:
                    conn.execute(update(table).where(
                        table.c.tenant_id == tenant,
                        table.c.connection_id == connection_id,
                        table.c.id.in_(active_ids),
                        table.c.archived_at.is_(None),
                    ).values(archived_at=now, archived_by=actor, archive_reason=reason))
            withdrawn = False
            inventory_withdrawals = conn.execute(update(self.inventory_concept_publications).where(
                self.inventory_concept_publications.c.tenant_id == tenant,
                self.inventory_concept_publications.c.connection_id == connection_id,
                self.inventory_concept_publications.c.concept_id.in_(newly_archived),
                self.inventory_concept_publications.c.revoked_at.is_(None),
            ).values(revoked_by=actor, revoked_at=now, revoke_reason="Archived proposal: " + reason[:980]))
            withdrawn = bool(inventory_withdrawals.rowcount)
            current_snapshot = conn.execute(select(
                self.snapshots.c.version, self.snapshots.c.payload,
            ).where(
                self.snapshots.c.tenant_id == tenant,
                self.snapshots.c.connection_id == connection_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).mappings().first()
            if current_snapshot is not None:
                current_version = current_snapshot["version"]
                self._replace_concept_search(
                    conn, tenant, connection_id, current_version, current_snapshot["payload"] or {},
                )
                result = conn.execute(update(self.publications).where(
                    self.publications.c.tenant_id == tenant,
                    self.publications.c.connection_id == connection_id,
                    self.publications.c.snapshot_version == current_version,
                    self.publications.c.revoked_at.is_(None),
                ).values(revoked_by=actor, revoked_at=now,
                         revoke_reason="Archived proposal: " + reason[:980]))
                snapshot_withdrawn = bool(result.rowcount)
                withdrawn = withdrawn or snapshot_withdrawn
                if snapshot_withdrawn:
                    self._audit(conn, tenant, connection_id, "source.withdraw_publication", actor, {
                        "snapshot_version": current_version,
                        "reason_digest": hashlib.sha256(reason.encode()).hexdigest(),
                        "trigger": "proposal_archive",
                    })
            self._audit(conn, tenant, connection_id, "proposal.archive", actor, {
                "proposal_ids": newly_archived, "approved_count": len(decisions),
                "reason_digest": hashlib.sha256(reason.encode()).hexdigest(),
            })
        return {"connection_id": connection_id, "archived_ids": newly_archived,
                "archived_count": len(newly_archived), "publication_withdrawn": withdrawn}

    def _validate_external_evidence_refs(
        self, tenant_id: str, target_connection_id: str, refs: list[dict[str, str]], *,
        allow_structured: bool = False,
    ) -> list[dict[str, str]]:
        if not isinstance(refs, list) or len(refs) > 8:
            raise KnowledgeValidationError("evidence_refs must contain at most 8 external citations")
        clean = []
        for ref in refs:
            from knowledge_layer.inventory_citations import (
                is_inventory_reference,
                resolve_inventory_citation,
            )

            if is_inventory_reference(ref):
                if allow_structured:
                    raise KnowledgeValidationError("Raw inventory citations cannot establish a cross-source join")
                if ref.get("connection_id") == target_connection_id:
                    raise KnowledgeValidationError("External evidence must come from another connection")
                resolve_inventory_citation(self, tenant_id, ref)
                clean.append(deepcopy(ref))
                continue
            if not isinstance(ref, dict) or set(ref) != {"connection_id", "snapshot_id", "evidence_id"}:
                raise KnowledgeValidationError("Each external evidence ref needs connection_id, snapshot_id, and evidence_id")
            other_id = ref["connection_id"]
            if not isinstance(other_id, str) or other_id == target_connection_id:
                raise KnowledgeValidationError("External evidence must come from another connection")
            other = self.get_snapshot(tenant_id, other_id)
            if not other or other.get("id") != ref["snapshot_id"]:
                raise KnowledgeConflict("The cited source has changed; search its current evidence")
            item = next((item for item in other.get("evidence", []) if isinstance(item, dict)
                         and item.get("id") == ref["evidence_id"]), None)
            allowed = {"document_derived_term", "upstream_semantic_definition"}
            if allow_structured:
                from knowledge_layer.cross_source import STRUCTURED_EVIDENCE

                allowed.update(STRUCTURED_EVIDENCE)
            if not item or item.get("evidence_type") not in allowed:
                raise KnowledgeValidationError("External evidence must identify a current document term or imported definition")
            if item.get("evidence_type") == "upstream_semantic_definition":
                from knowledge_layer.federated import _upstream_asset

                if _upstream_asset(other, item) is None:
                    raise KnowledgeValidationError("Imported evidence must identify an exact current native binding")
            clean.append({key: ref[key] for key in ("connection_id", "snapshot_id", "evidence_id")})
        if len({tuple(sorted(ref.items())) for ref in clean}) != len(clean):
            raise KnowledgeValidationError("External evidence refs must be distinct")
        return clean

    def _contract_dependencies_current(self, tenant_id: str, proposal: dict[str, Any]) -> bool:
        if proposal.get("assertion_kind") == "term_binding":
            return self._term_binding_target_current(
                tenant_id, proposal.get("payload") or {}, proposal.get("evidence_refs") or [],
            )
        return self._external_evidence_current(tenant_id, proposal.get("evidence_refs") or [])

    def _term_binding_target_current(
        self, tenant_id: str, payload: dict[str, Any], refs: list[dict[str, str]], *, require_review: bool = True,
    ) -> bool:
        """Check an exact target and its local meaning without dependency recursion."""
        from knowledge_layer.contracts import (
            reviewed_term_binding_target,
            validate_term_binding_target,
        )

        target_id = payload.get("target_connection_id", "")
        if not self._has_access(tenant_id, target_id):
            return False
        with self.engine.connect() as conn:
            row = conn.execute(select(
                self.snapshots.c.snapshot_id, self.snapshots.c.payload, self.connections.c.status,
            ).join(self.connections, self.snapshots.c.connection_id == self.connections.c.id).where(
                self.snapshots.c.tenant_id == tenant_id, self.snapshots.c.connection_id == target_id,
            ).order_by(self.snapshots.c.version.desc()).limit(1)).first()
            if not row or row.snapshot_id != payload.get("target_snapshot_id") or row.status == "error":
                return False
            target = deepcopy(row.payload or {})
            target["id"] = row.snapshot_id
            try:
                validate_term_binding_target(target, payload, refs)
            except (KnowledgeValidationError, KeyError, TypeError):
                return False
            if not require_review:
                return True
            reviews = conn.execute(select(
                self.proposals, self.decisions.c.proposal_digest, self.decisions.c.decision,
                self.decisions.c.decided_at,
            ).join(self.decisions, and_(
                self.decisions.c.tenant_id == self.proposals.c.tenant_id,
                self.decisions.c.connection_id == self.proposals.c.connection_id,
                self.decisions.c.proposal_id == self.proposals.c.id,
            )).where(
                self.proposals.c.tenant_id == tenant_id, self.proposals.c.connection_id == target_id,
                self.proposals.c.archived_at.is_(None), self.proposals.c.proposal_type == "semantic_correction",
                self.proposals.c.target_concept_id == payload["target_concept_id"],
                self.decisions.c.decision == "approve",
            ).order_by(self.proposals.c.created_at.asc()).limit(MAX_SOURCE_PROPOSALS + 1)).mappings().all()
            if len(reviews) > MAX_SOURCE_PROPOSALS:
                return False
        # Only the local semantic ledger is projected here. Following target
        # contracts would recurse through unrelated cross-source dependencies.
        target["proposals"] = [_semantic_proposal_response(dict(review)) for review in reviews]
        projected = self._apply_decisions(tenant_id, target_id, target,
            recorded_decisions={review["id"]: dict(review) for review in reviews})
        return reviewed_term_binding_target(projected, payload, refs)

    def _external_evidence_current(self, tenant_id: str, refs: list[dict[str, str]]) -> bool:
        """Check raw current source revisions without recursively projecting decisions."""
        if not refs:
            return True
        with self.engine.connect() as conn:
            for ref in refs:
                from knowledge_layer.inventory_citations import (
                    InventoryCitationBudgetExceeded,
                    is_inventory_reference,
                    resolve_inventory_citation,
                )

                if is_inventory_reference(ref):
                    try:
                        resolve_inventory_citation(self, tenant_id, ref)
                    except InventoryCitationBudgetExceeded:
                        raise
                    except KnowledgeError:
                        return False
                    continue
                if not self._has_access(tenant_id, ref.get("connection_id", "")):
                    return False
                row = conn.execute(
                    select(self.snapshots.c.snapshot_id, self.snapshots.c.payload, self.connections.c.status)
                    .join(self.connections, self.snapshots.c.connection_id == self.connections.c.id)
                    .where(self.snapshots.c.tenant_id == tenant_id,
                           self.snapshots.c.connection_id == ref.get("connection_id"))
                    .order_by(self.snapshots.c.version.desc()).limit(1)
                ).first()
                if not row or row.snapshot_id != ref.get("snapshot_id") or row.status == "error":
                    return False
                evidence = (row.payload or {}).get("evidence", [])
                item = next((item for item in evidence if isinstance(item, dict)
                             and item.get("id") == ref.get("evidence_id")), None)
                if not item:
                    return False
                from knowledge_layer.cross_source import (
                    STRUCTURED_EVIDENCE,
                    structured_reference_current,
                )

                if item.get("evidence_type") in STRUCTURED_EVIDENCE and not structured_reference_current(self, tenant_id, ref):
                    return False
                if item.get("evidence_type") == "upstream_semantic_definition":
                    from knowledge_layer.connectors.common import stable_id
                    from knowledge_layer.federated import _upstream_asset

                    asset = _upstream_asset(row.payload or {}, item)
                    if asset is None:
                        return False
                    target = stable_id("concept", asset["id"], "upstream_definition")
                    concept = next((concept for concept in (row.payload or {}).get("concepts", [])
                                    if isinstance(concept, dict) and concept.get("id") == target
                                    and concept.get("upstream_definition") == asset["upstream_definition"]
                                    and (concept.get("source_binding") or {}).get("asset_id") == asset["id"]), None)
                    if concept is None:
                        return False
                    # Read the local ledger without get_snapshot: projecting a
                    # cross-source contract here would recurse into this check.
                    reviews = conn.execute(select(self.proposals, self.decisions.c.proposal_digest,
                                                   self.decisions.c.decision, self.decisions.c.decided_at)
                        .join(self.decisions, and_(
                            self.decisions.c.tenant_id == self.proposals.c.tenant_id,
                            self.decisions.c.connection_id == self.proposals.c.connection_id,
                            self.decisions.c.proposal_id == self.proposals.c.id,
                        )).where(
                            self.proposals.c.tenant_id == tenant_id,
                            self.proposals.c.connection_id == ref["connection_id"],
                            self.proposals.c.archived_at.is_(None),
                            self.proposals.c.proposal_type == "semantic_correction",
                            self.proposals.c.target_concept_id == target,
                            self.decisions.c.decision == "approve",
                        ).order_by(self.proposals.c.created_at.asc()).limit(MAX_SOURCE_PROPOSALS + 1)).mappings().all()
                    if len(reviews) > MAX_SOURCE_PROPOSALS:
                        return False
                    semantic = deepcopy(row.payload or {})
                    semantic["proposals"] = [_semantic_proposal_response(dict(review)) for review in reviews]
                    projected = self._apply_decisions(tenant_id, ref["connection_id"], semantic,
                        recorded_decisions={review["id"]: dict(review) for review in reviews})
                    from knowledge_layer.federated import _reviewed_upstream_concepts

                    if not _reviewed_upstream_concepts(projected, item):
                        return False
        return True

    def _connection_response(self, row: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
        clean_config = _sanitize(row.get("config") or {})
        inventory_mode = row["kind"] == "external_adapter" and clean_config.get("discovery_mode") == "inventory"
        stale_assertion_count = sum(
            1 for item in (snapshot or {}).get("proposals", [])
            if isinstance(item, dict) and item.get("proposal_type") == "contract_assertion"
            and (item.get("status") == "stale_review" or
                 (item.get("status") == "approved" and item.get("effective_on_current_snapshot") is False))
        )
        counts = {
            "assets": len((snapshot or {}).get("assets", [])),
            "concepts": len((snapshot or {}).get("concepts", [])),
            "relationships": len((snapshot or {}).get("relationships", [])),
            "proposals": len((snapshot or {}).get("proposals", [])),
        }
        if inventory_mode and snapshot is None:
            counts = dict.fromkeys(counts)
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "kind": row["kind"],
            "connector_type": row["kind"],
            "name": row["name"],
            "config": clean_config,
            "status": row["status"],
            "discovery_mode": "inventory" if inventory_mode else "snapshot",
            "discovery_complete": None if inventory_mode else (snapshot or {}).get("discovery_complete") is True,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_refreshed_at": row.get("last_refreshed_at"),
            "last_synced_at": row.get("last_refreshed_at"),
            "last_refresh_attempt_at": row.get("last_refresh_attempt_at"),
            "last_complete_refresh_at": row.get("last_complete_refresh_at"),
            "last_error": row.get("last_error"),
            "error": row.get("last_error"),
            "asset_count": counts["assets"],
            "concept_count": counts["concepts"],
            "relationship_count": counts["relationships"],
            "proposal_count": counts["proposals"],
            "agent_ready": (snapshot or {}).get("agent_ready") is True,
            "stale_assertion_count": stale_assertion_count,
            "needs_attention_count": stale_assertion_count + sum(1 for item in (snapshot or {}).get("proposals", []) if isinstance(item, dict) and item.get("status") in {"candidate", "pending", "pending_review", "needs_review"}),
            "stage": (
                "inventory_complete" if inventory_mode and row["status"] == "inventory_complete" else
                "needs_review" if stale_assertion_count else
                "agent_ready" if (snapshot or {}).get("agent_ready") is True
                else "discovery_complete" if (snapshot or {}).get("discovery_complete") is True or (snapshot or {}).get("status") == "discovery_complete"
                else "needs_review" if snapshot
                else "configured"
            ),
            "coverage": {"assets": counts["assets"], "concepts": counts["concepts"]},
            "counts": counts,
            "snapshot": snapshot,
        }

    def _mark_error(self, tenant_id: str, connection_id: str) -> None:
        now = utc_now()
        with self.engine.begin() as conn:
            conn.execute(select(self.connections.c.id).where(
                self.connections.c.tenant_id == tenant_id, self.connections.c.id == connection_id,
            ).with_for_update()).first()
            from knowledge_layer.refresh_fence import assert_refresh_fence

            assert_refresh_fence(self, conn, tenant_id, connection_id)
            conn.execute(
                update(self.connections)
                .where(self.connections.c.tenant_id == tenant_id, self.connections.c.id == connection_id)
                .values(status="error", updated_at=now, last_error="Curation failed; adapter details are not stored.")
            )
            assert_refresh_fence(self, conn, tenant_id, connection_id)

    def _list_local_proposals(self, tenant_id: str, connection_id: str, *, as_of: str | None = None) -> list[dict[str, Any]]:
        statement = select(self.proposals).where(
            self.proposals.c.tenant_id == tenant_id,
            self.proposals.c.connection_id == connection_id,
        )
        if as_of is not None:
            statement = statement.where(
                self.proposals.c.created_at <= as_of,
                or_(self.proposals.c.archived_at.is_(None), self.proposals.c.archived_at > as_of),
            )
        else:
            statement = statement.where(self.proposals.c.archived_at.is_(None))
        with self.engine.connect() as conn:
            rows = conn.execute(statement.order_by(self.proposals.c.created_at.asc()).limit(
                MAX_SOURCE_PROPOSALS + 1
            )).mappings().all()
        if len(rows) > MAX_SOURCE_PROPOSALS:
            raise KnowledgeConflict("Source proposal ledger exceeds the supported limit")
        return [_semantic_proposal_response(dict(row)) for row in rows]

    def _list_contract_proposals(self, tenant_id: str, connection_id: str, *, as_of: str | None = None) -> list[dict[str, Any]]:
        statement = select(self.contract_proposals).where(
            self.contract_proposals.c.tenant_id == tenant_id,
            self.contract_proposals.c.connection_id == connection_id,
        )
        if as_of is not None:
            statement = statement.where(
                self.contract_proposals.c.created_at <= as_of,
                or_(self.contract_proposals.c.archived_at.is_(None), self.contract_proposals.c.archived_at > as_of),
            )
        else:
            statement = statement.where(self.contract_proposals.c.archived_at.is_(None))
        with self.engine.connect() as conn:
            rows = conn.execute(statement.order_by(self.contract_proposals.c.created_at.asc()).limit(
                MAX_SOURCE_PROPOSALS + 1
            )).mappings().all()
        if len(rows) > MAX_SOURCE_PROPOSALS:
            raise KnowledgeConflict("Source proposal ledger exceeds the supported limit")
        return [_contract_proposal_response(dict(row)) for row in rows]

    def _local_proposal_ids(self, tenant_id: str, connection_id: str, candidate_ids: list[str]) -> set[str]:
        """Identify local rows embedded in old snapshot payloads, including archived ones."""
        result: set[str] = set()
        unique_ids = list(dict.fromkeys(item for item in candidate_ids if item))
        with self.engine.connect() as conn:
            for offset in range(0, len(unique_ids), 500):
                chunk = unique_ids[offset:offset + 500]
                for table in (self.proposals, self.contract_proposals):
                    result.update(conn.execute(select(table.c.id).where(
                        table.c.tenant_id == tenant_id,
                        table.c.connection_id == connection_id,
                        table.c.id.in_(chunk),
                    )).scalars())
        return result

    def _apply_decisions(
        self, tenant_id: str, connection_id: str, snapshot: dict[str, Any], *,
        as_of: str | None = None, recorded_decisions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = deepcopy(snapshot)
        from knowledge_layer.contracts import strip_materialized_assertions

        strip_materialized_assertions(result)
        if recorded_decisions is None:
            statement = select(self.decisions).where(
                        self.decisions.c.tenant_id == tenant_id,
                        self.decisions.c.connection_id == connection_id,
                    )
            if as_of is not None:
                statement = statement.where(self.decisions.c.decided_at <= as_of)
            with self.engine.connect() as conn:
                rows = conn.execute(statement).mappings().all()
            recorded = {row["proposal_id"]: dict(row) for row in rows}
        else:
            recorded = recorded_decisions
        by_id: dict[str, dict[str, Any]] = {}
        result["review_integrity_failed"] = False
        for item in result.get("proposals", []):
            if not isinstance(item, dict):
                continue
            proposal_id = _record_id(item)
            decision = recorded.get(proposal_id)
            if not decision:
                continue
            if decision.get("proposal_digest") != _proposal_digest(item, result.get("evidence", [])):
                result["review_integrity_failed"] = True
                item["status"] = "stale_review"
                item["effective_on_current_snapshot"] = False
                continue
            by_id[proposal_id] = decision
        approved_ids = {key for key, row in by_id.items() if row["decision"] == "approve"}
        # A review snapshot may have stored read-time semantic overlays. Remove
        # those whose local proposal is now archived before recomputing meaning.
        for concept in result.get("concepts", []):
            if not isinstance(concept, dict):
                continue
            if concept.get("definition_proposal_id") and concept["definition_proposal_id"] not in approved_ids:
                for key in ("definition", "definition_status", "definition_proposal_id",
                            "definition_evidence_ids", "definition_decided_at",
                            "definition_decision_rationale"):
                    concept.pop(key, None)
            if concept.get("mapping_proposal_id") and concept["mapping_proposal_id"] not in approved_ids:
                for key in ("approved_mapping", "mapping_status", "mapping_proposal_id",
                            "mapping_evidence_ids"):
                    concept.pop(key, None)
        result["relationships"] = [item for item in result.get("relationships", [])
                                   if not (isinstance(item, dict) and
                                           str(item.get("id", "")).startswith("decision:"))]
        # Older snapshots may contain provider-supplied approval labels. Only the
        # separate owner-decision ledger can make an assertion approved.
        for collection in ("assets", "concepts", "relationships", "proposals", "metrics"):
            for item in result.get(collection, []):
                if not isinstance(item, dict):
                    continue
                item["certified"] = False
                marker = (item.get("decision_proposal_id") or item.get("proposal_id")
                          or (_record_id(item) if collection == "proposals" else None))
                if item.get("status") == "approved" and marker not in approved_ids:
                    item["status"] = "candidate"
                if collection == "proposals" and item.get("status") == "rejected" and marker not in by_id:
                    item["status"] = "pending_review"
                if collection == "concepts":
                    if item.get("definition_status") == "approved" and item.get("definition_proposal_id") not in approved_ids:
                        item["definition_status"] = "candidate"
                    if item.get("mapping_status") == "approved" and item.get("mapping_proposal_id") not in approved_ids:
                        item["mapping_status"] = "candidate"
                        item.pop("approved_mapping", None)
                if collection == "assets":
                    if item.get("description_status") == "approved" and item.get("description_proposal_id") not in approved_ids:
                        item["description_status"] = "candidate"
                    for field in item.get("schema", []):
                        if (isinstance(field, dict) and field.get("description_status") == "approved"
                                and field.get("description_proposal_id") not in approved_ids):
                            field["description_status"] = "candidate"
        proposals = result.setdefault("proposals", [])
        relationships = result.setdefault("relationships", [])
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            proposal_id = _record_id(proposal)
            decision = by_id.get(proposal_id)
            if not decision:
                continue
            proposal["status"] = "approved" if decision["decision"] == "approve" else "rejected"
            proposal["effective_on_current_snapshot"] = False
            proposal["certified"] = False
            proposal["decision"] = decision["decision"]
            proposal["decision_rationale"] = decision.get("rationale")
            proposal["decided_at"] = decision.get("decided_at")
            subject = proposal.get("subject_id")
            object_id = proposal.get("object_id")
            predicate = proposal.get("predicate") or "related_to"
            mapping = proposal.get("proposed_mapping") if isinstance(proposal.get("proposed_mapping"), dict) else {}
            if (mapping and proposal.get("proposal_type") == "semantic_correction"
                    and decision["decision"] == "approve"
                    and not _semantic_mapping_current(result, mapping, _evidence_ids(proposal))):
                result["review_integrity_failed"] = True
                proposal["status"] = "stale_review"
                continue
            subject = subject or proposal.get("target_concept_id") or mapping.get("subject_id")
            object_id = object_id or mapping.get("object_id")
            predicate = proposal.get("predicate") or mapping.get("predicate") or "related_to"
            evidence_ids = _evidence_ids(proposal)
            if decision["decision"] == "approve" and proposal.get("proposal_type") == "contract_assertion":
                from knowledge_layer.contracts import apply_approved_assertion

                dependency_current = self._contract_dependencies_current(tenant_id, proposal)
                if proposal.get("assertion_kind") == "cross_source_join" and dependency_current:
                    from knowledge_layer.cross_source import target_binding_current

                    dependency_current = target_binding_current(self, tenant_id, proposal.get("payload") or {})

                apply_approved_assertion(
                    result, proposal,
                    external_evidence_current=dependency_current,
                )
            if decision["decision"] == "approve" and proposal.get("proposal_type") == "semantic_correction":
                target_id = str(proposal.get("target_concept_id") or subject or "")
                for concept in result.get("concepts", []):
                    if not isinstance(concept, dict) or _record_id(concept) != target_id:
                        continue
                    definition = proposal.get("proposed_definition")
                    if definition:
                        concept["definition"] = definition
                        concept["definition_status"] = "approved"
                        concept["definition_proposal_id"] = proposal_id
                        concept["definition_evidence_ids"] = evidence_ids
                        concept["definition_decided_at"] = decision.get("decided_at")
                        concept["definition_decision_rationale"] = decision.get("rationale") or proposal.get("rationale")
                    if mapping:
                        concept["approved_mapping"] = deepcopy(mapping)
                        concept["mapping_status"] = "approved"
                        concept["mapping_proposal_id"] = proposal_id
                        concept["mapping_evidence_ids"] = evidence_ids
                    proposal["effective_on_current_snapshot"] = bool(definition or mapping)
                    break
            current_nodes = {_record_id(item) for collection in ("assets", "concepts")
                             for item in result.get(collection, []) if isinstance(item, dict)}
            relation_current = subject in current_nodes and object_id in current_nodes
            existing_relation = next((
                relation for relation in relationships
                if isinstance(relation, dict)
                and relation.get("subject_id") == subject
                and relation.get("object_id") == object_id
                and relation.get("predicate") == predicate
            ), None) if relation_current else None
            if existing_relation:
                existing_relation.update({
                    "status": proposal["status"], "decision_proposal_id": proposal_id,
                    "evidence_ids": list(dict.fromkeys(_evidence_ids(existing_relation) + evidence_ids)),
                    "certified": False,
                })
                if decision["decision"] == "approve":
                    proposal["effective_on_current_snapshot"] = True
            elif relation_current and decision["decision"] == "approve":
                relationships.append({
                    "id": f"decision:{proposal_id}",
                    "subject_id": subject,
                    "predicate": predicate,
                    "object_id": object_id,
                    "status": proposal["status"],
                    "confidence": proposal.get("confidence"),
                    "rationale": decision.get("rationale") or proposal.get("rationale"),
                    "evidence_ids": evidence_ids,
                    "decision_proposal_id": proposal_id,
                    "certified": False,
                })
                proposal["effective_on_current_snapshot"] = True
        if any(
            isinstance(item, dict) and item.get("proposal_type") == "contract_assertion"
            and item.get("status") == "approved"
            and item.get("effective_on_current_snapshot") is False
            for item in proposals
        ):
            result["agent_ready"] = False
            result["meaning_certified"] = False
            result["curation_status"] = "stale_evidence_review_required"
        return result

    def _context_for_concept(
        self, tenant_id: str, concept: dict[str, Any], all_concepts: list[dict[str, Any]] | None = None,
        connection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        connection_id = str(concept.get("connection_id") or "")
        if connection is None:
            connection = self.get_connection(tenant_id, connection_id) if connection_id else None
        if connection is None or connection.get("id") != connection_id:
            raise KnowledgeConflict("Source context changed during assembly; reload current meaning")
        snapshot = connection.get("snapshot") or {}
        concept_id = _record_id(concept)
        current = next((
            {**item, "id": concept_id, "connection_id": connection_id, "connection_name": connection["name"]}
            for item in snapshot.get("concepts", [])
            if isinstance(item, dict) and _record_id(item) == concept_id
        ), None)
        if current != concept:
            raise KnowledgeConflict("Source context changed during assembly; reload current meaning")
        relationships = [
            relationship for relationship in snapshot.get("relationships", [])
            if isinstance(relationship, dict) and concept_id in {str(relationship.get("subject_id") or ""), str(relationship.get("object_id") or "")}
        ][:20]
        related_ids = {
            str(relationship.get("object_id") if str(relationship.get("subject_id")) == concept_id else relationship.get("subject_id"))
            for relationship in relationships
        } - {"", concept_id}
        related = [
            {**item, "id": _record_id(item)}
            for item in snapshot.get("concepts", [])
            if isinstance(item, dict) and _record_id(item) in related_ids
        ][:10]
        evidence_ids = list(dict.fromkeys(_evidence_ids(concept) + [value for relation in relationships for value in _evidence_ids(relation)]))[:20]
        evidence_index = {_record_id(row): row for row in snapshot.get("evidence", []) if isinstance(row, dict)}
        evidence = [
            {"id": evidence_id, "summary": evidence_index[evidence_id].get("summary"), "source_pointer": evidence_index[evidence_id].get("source_pointer"), "evidence_type": evidence_index[evidence_id].get("evidence_type")}
            for evidence_id in evidence_ids if evidence_id in evidence_index
        ]
        source = snapshot.get("source") or {}
        label = normalize_name(str(concept.get("name") or ""))
        cross_source_candidates = [
            {
                "concept_id": candidate["id"], "name": candidate.get("name"),
                "connection_id": candidate["connection_id"], "connection_name": candidate["connection_name"],
                "status": "possible_shared_term", "certified": False,
                "rationale": "The normalized label and semantic type match across sources; equivalence and scope need review.",
            }
            for candidate in (all_concepts if all_concepts is not None else self.get_concepts(tenant_id))
            if label and candidate["connection_id"] != connection_id
            and normalize_name(str(candidate.get("name") or "")) == label
            and candidate.get("semantic_type") == concept.get("semantic_type")
        ][:5]
        return {
            "tenant_id": tenant_id,
            "concept": concept,
            "trust_state": concept.get("trust_state") or concept.get("status"),
            "snapshot": {
                "id": snapshot.get("id"),
                "version": snapshot.get("version"),
                "created_at": snapshot.get("created_at"),
            },
            "snapshot_id": snapshot.get("id"),
            "snapshot_version": snapshot.get("version"),
            "snapshot_created_at": snapshot.get("created_at"),
            "evidence_ids": evidence_ids,
            "evidence": evidence,
            "source_bindings": [{
                "connection_id": connection_id,
                "connection_name": (connection or {}).get("name"),
                "kind": (connection or {}).get("kind"),
                "connection_status": (connection or {}).get("status"),
                "last_error": (connection or {}).get("last_error"),
                "last_refreshed_at": (connection or {}).get("last_refreshed_at"),
                "last_refresh_attempt_at": (connection or {}).get("last_refresh_attempt_at"),
                "last_complete_refresh_at": (connection or {}).get("last_complete_refresh_at"),
                "source": source,
            }],
            "relationships": relationships,
            "related_concepts": related,
            "cross_source_candidates": cross_source_candidates,
        }


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _record_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("concept_id") or row.get("evidence_id") or row.get("proposal_id") or "")


def _evidence_ids(row: dict[str, Any]) -> list[str]:
    values = row.get("evidence_ids")
    if values is None and row.get("evidence_id") is not None:
        values = [row["evidence_id"]]
    if isinstance(values, str):
        values = [values]
    return [str(value) for value in values or [] if value]


def _semantic_mapping_current(
    snapshot: dict[str, Any], mapping: dict[str, Any], evidence_ids: list[str]
) -> bool:
    concepts = {_record_id(item) for item in snapshot.get("concepts", []) if isinstance(item, dict)}
    if mapping.get("subject_id") not in concepts or mapping.get("object_id") not in concepts:
        return False
    relation = next((item for item in snapshot.get("relationships", [])
                     if isinstance(item, dict) and _record_id(item) == mapping.get("relationship_id")), None)
    return bool(
        relation
        and all(mapping.get(key) == relation.get(key)
                for key in ("subject_id", "predicate", "object_id"))
        and set(evidence_ids).intersection(_evidence_ids(relation))
    )


def _semantic_proposal_response(row: dict[str, Any]) -> dict[str, Any]:
    mapping = row.get("proposed_mapping") if isinstance(row.get("proposed_mapping"), dict) else None
    result = {
        "id": row.get("id"),
        "proposal_type": row.get("proposal_type", "semantic_correction"),
        "target_concept_id": row.get("target_concept_id"),
        "subject_id": row.get("target_concept_id"),
        "proposed_definition": row.get("proposed_definition"),
        "proposed_mapping": deepcopy(mapping),
        "evidence_ids": list(row.get("evidence_ids") or []),
        "rationale": row.get("rationale"),
        "status": row.get("status", "pending_review"),
        "certified": False,
        "created_at": row.get("created_at"),
    }
    if mapping:
        for field in ("predicate", "object_id"):
            if mapping.get(field) is not None:
                result[field] = mapping[field]
    for field in ("decision", "decision_rationale", "decided_at"):
        if row.get(field) is not None:
            result[field] = row[field]
    return result


def _contract_proposal_response(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": row.get("id"), "proposal_type": "contract_assertion",
        "assertion_kind": row.get("assertion_kind"), "asset_id": row.get("asset_id"),
        "source_snapshot_id": row.get("source_snapshot_id"),
        "payload": deepcopy(row.get("payload") or {}),
        "evidence_ids": list(row.get("evidence_ids") or []),
        "evidence_refs": deepcopy(row.get("evidence_refs") or []),
        "rationale": row.get("rationale"), "status": row.get("status", "pending_review"),
        "certified": False, "created_at": row.get("created_at"),
    }
    for field in ("decision", "decision_rationale", "decided_at"):
        if row.get(field) is not None:
            result[field] = row[field]
    return result
