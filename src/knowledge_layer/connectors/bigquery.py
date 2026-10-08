"""BigQuery table/schema metadata via fixed read-only REST resources.

The optional Google auth library signs a service-account assertion. All actual
HTTP requests, including token exchange, use one bounded transport. No query,
tabledata, storage-read, upload or source-write API is available here.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx

from knowledge_layer.connectors.cloud_metadata import (
    CONNECT_TIMEOUT,
    MetadataBudget,
    asset,
    discovery,
    scope_config,
    secret_object,
    text,
)
from knowledge_layer.connectors.common import ConnectorError, stable_id, type_name

_SCOPE = r"[a-z][a-z0-9-]{4,61}[a-z0-9]\.[A-Za-z_][A-Za-z0-9_]*"
_API = "https://bigquery.googleapis.com/bigquery/v2/projects/"
_TOKEN = "https://oauth2.googleapis.com/token"
_FIELDS = "tableReference,type,description,schema,creationTime,timePartitioning,rangePartitioning,clustering,requirePartitionFilter"
MAX_PAGES = 10
MAX_FIELD_DEPTH = 15


def _raw_response(client: httpx.Client, method: str, url: str, budget: MetadataBudget, **kwargs: Any) -> tuple[int, bytes, dict[str, str]]:
    remaining = budget.timeout()
    with client.stream(method, url, timeout=httpx.Timeout(remaining, connect=min(CONNECT_TIMEOUT, remaining)), **kwargs) as response:
        if 300 <= response.status_code < 400:
            raise ConnectorError("cloud_endpoint_denied", "Cloud metadata redirects are not allowed")
        parts = []
        for chunk in response.iter_bytes(64 * 1024):
            budget.consume_bytes(len(chunk))
            parts.append(chunk)
        budget.timeout()
        return response.status_code, b"".join(parts), dict(response.headers)


def _access_token(profile: dict[str, Any], budget: MetadataBudget, client: httpx.Client) -> str:
    if profile.get("type") == "short_lived_access_token":
        if set(profile) != {"type", "access_token", "expires_at"}:
            raise ConnectorError("database_secret_invalid", "The short-lived credential has unknown fields")
        try:
            expiry = datetime.fromisoformat(profile["expires_at"])
            token = profile["access_token"]
            if (expiry.tzinfo is None or not isinstance(token, str)
                    or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,32768}", token)
                    or not 30 < (expiry.astimezone(UTC) - datetime.now(UTC)).total_seconds() <= 3600):
                raise ValueError
        except (TypeError, ValueError, KeyError, OverflowError):
            raise ConnectorError("database_credentials_expired", "Rotate the short-lived BigQuery credential") from None
        budget.timeout()
        return token
    if profile.get("type") in {"application_default_credentials", "adc"}:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:
            raise ConnectorError("sql_driver_unavailable", "Install the bigquery optional extra for native metadata authentication") from exc
        try:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/bigquery.readonly"])
            credentials.refresh(Request())
            budget.timeout()
            return text(credentials.token, maximum=32_768)
        except Exception as exc:
            raise ConnectorError("database_credentials_unavailable", "Failed to acquire token from Application Default Credentials") from exc
    allowed = {"type", "project_id", "private_key_id", "private_key", "client_email", "client_id",
               "auth_uri", "token_uri", "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain"}
    required = {"type", "project_id", "private_key", "client_email", "token_uri"}
    if not required <= set(profile) or not set(profile) <= allowed or profile["type"] != "service_account":
        raise ConnectorError("database_secret_invalid", "BigQuery requires a server-side service-account JSON credential")
    if any(not isinstance(value, str) or not value for value in profile.values()) or profile["token_uri"] != _TOKEN:
        raise ConnectorError("database_secret_invalid", "The BigQuery service-account credential has invalid or unsupported values")
    if profile.get("universe_domain", "googleapis.com") != "googleapis.com":
        raise ConnectorError("database_secret_invalid", "This BigQuery connector requires the public Google API universe")
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise ConnectorError("sql_driver_unavailable", "Install the bigquery optional extra for native metadata authentication") from exc

    def request(url: str, method: str = "GET", body: bytes | None = None,
                headers: dict[str, str] | None = None, **_kwargs: Any) -> Any:
        if url != _TOKEN or method != "POST":
            raise ConnectorError("cloud_endpoint_denied", "The cloud credential attempted an unapproved token endpoint")
        status, data, response_headers = _raw_response(client, method, url, budget, content=body, headers=headers)
        return SimpleNamespace(status=status, data=data, headers=response_headers)

    credentials = service_account.Credentials.from_service_account_info(
        profile, scopes=["https://www.googleapis.com/auth/bigquery.readonly"])
    credentials.refresh(request)
    return text(credentials.token, maximum=32_768)


def _get(client: httpx.Client, path: str, token: str, budget: MetadataBudget, params: dict[str, Any]) -> dict[str, Any]:
    status, data, _headers = _raw_response(client, "GET", _API + path, budget,
        headers={"Authorization": "Bearer " + token}, params=params)
    if status in {401, 403}:
        raise ConnectorError("database_access_denied", "BigQuery metadata access was denied")
    if status == 404:
        raise ConnectorError("table_not_found", "The selected BigQuery metadata scope is unavailable")
    if status != 200:
        raise ConnectorError("sql_metadata_unavailable", "BigQuery metadata discovery failed")
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ConnectorError("cloud_metadata_invalid", "BigQuery returned invalid metadata JSON") from exc
    if not isinstance(payload, dict):
        raise ConnectorError("cloud_metadata_invalid", "BigQuery returned invalid metadata JSON")
    return payload


def _table_reference(value: Any, project: str, dataset: str) -> str:
    if not isinstance(value, dict) or value.get("projectId") != project or value.get("datasetId") != dataset:
        raise ConnectorError("cloud_scope_mismatch", "BigQuery returned metadata outside the selected scope")
    return text(value.get("tableId"), maximum=1024)


def _schema(rows: Any, budget: MetadataBudget, count: list[int], maximum: int, depth: int = 1) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or depth > MAX_FIELD_DEPTH:
        raise ConnectorError("cloud_metadata_invalid", "BigQuery returned invalid or excessively nested schema metadata")
    fields = []
    names = set()
    for row in rows:
        count[0] += 1
        if count[0] > maximum:
            raise ConnectorError("column_limit_exceeded", "The selected BigQuery scope exceeds max_columns, including nested fields")
        if not isinstance(row, dict):
            raise ConnectorError("cloud_metadata_invalid", "BigQuery returned invalid schema metadata")
        name, native_type = text(row.get("name"), maximum=1200), text(row.get("type"), maximum=256)
        mode = row.get("mode", "NULLABLE")
        if name in names or mode not in {"NULLABLE", "REQUIRED", "REPEATED"}:
            raise ConnectorError("cloud_metadata_invalid", "BigQuery returned duplicate fields or invalid modes")
        names.add(name)
        nested = _schema(row.get("fields", []), budget, count, maximum, depth + 1) if native_type in {"RECORD", "STRUCT"} else None
        if nested is None and row.get("fields"):
            raise ConnectorError("cloud_metadata_invalid", "BigQuery returned inconsistent nested schema metadata")
        physical = {key: row[key] for key in ("precision", "scale", "maxLength", "roundingMode", "rangeElementType") if key in row}
        policy_tags = row.get("policyTags", {}).get("names", []) if isinstance(row.get("policyTags", {}), dict) else None
        if not isinstance(policy_tags, list) or len(policy_tags) > 20:
            raise ConnectorError("cloud_metadata_invalid", "BigQuery returned invalid declared field policies")
        policy_tags = [text(tag, maximum=2048) for tag in policy_tags]
        field = {"name": name, "data_type": "structured" if nested is not None or mode == "REPEATED" else type_name(native_type),
                 "native_type": native_type, "nullable": mode != "REQUIRED", "native_mode": mode,
                 "source_description": text(row.get("description"), optional=True),
                 "native_type_parameters": physical, "declared_policy_tags": policy_tags}
        if nested is not None:
            field["fields"] = nested
        budget.consume_json(field)
        fields.append(field)
    return fields


def discover(config: dict[str, Any]) -> dict[str, Any]:
    reference, scope, selected, max_tables, max_columns = scope_config(config, "bigquery", _SCOPE)
    project, dataset = scope.split(".")
    profile = secret_object(reference, "bigquery")
    budget = MetadataBudget()
    source_id = config.get("source_id") or stable_id("source", "sql_catalog", "bigquery", scope, config.get("name"))
    base = quote(project, safe="") + "/datasets/" + quote(dataset, safe="") + "/tables"
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            token = _access_token(profile, budget, client)
            names = set(selected or [])
            if selected is None:
                page_token = None
                seen_tokens = set()
                for _page in range(MAX_PAGES):
                    params: dict[str, Any] = {"maxResults": min(100, max_tables - len(names) + 1),
                                              "fields": "tables(tableReference,type),nextPageToken"}
                    if page_token is not None:
                        params["pageToken"] = page_token
                    payload = _get(client, base, token, budget, params)
                    rows = payload.get("tables", [])
                    if not isinstance(rows, list) or len(rows) > params["maxResults"]:
                        raise ConnectorError("table_limit_exceeded", "The selected BigQuery scope exceeds max_tables")
                    for row in rows:
                        name = _table_reference(row.get("tableReference") if isinstance(row, dict) else None, project, dataset)
                        if name in names:
                            raise ConnectorError("cloud_metadata_invalid", "BigQuery returned duplicate table identities")
                        names.add(name)
                    if len(names) > max_tables:
                        raise ConnectorError("table_limit_exceeded", "The selected BigQuery scope exceeds max_tables")
                    page_token = payload.get("nextPageToken")
                    if page_token in (None, ""):
                        break
                    page_token = text(page_token, maximum=4096)
                    if not rows or page_token in seen_tokens:
                        raise ConnectorError("cloud_metadata_invalid", "BigQuery returned an invalid metadata continuation")
                    seen_tokens.add(page_token)
                else:
                    raise ConnectorError("table_limit_exceeded", "The BigQuery table listing exceeds its finite page budget; select exact tables")
            assets = []
            field_count = [0]
            for name in sorted(names):
                payload = _get(client, base + "/" + quote(name, safe=""), token, budget, {"view": "BASIC", "fields": _FIELDS})
                if _table_reference(payload.get("tableReference"), project, dataset) != name:
                    raise ConnectorError("cloud_scope_mismatch", "BigQuery returned an unexpected table identity")
                schema = _schema((payload.get("schema") or {}).get("fields"), budget, field_count, max_columns)
                if not schema:
                    raise ConnectorError("cloud_metadata_invalid", "A selected BigQuery table has no visible schema")
                observations = {
                    "native_table_type": text(payload.get("type"), maximum=128),
                    "source_description": text(payload.get("description"), optional=True),
                    "native_created_at": text(payload.get("creationTime"), maximum=128, optional=True),
                    "native_schema": schema, "application_rows_read": False,
                    "declared_partitioning": {key: payload[key] for key in
                        ("timePartitioning", "rangePartitioning", "clustering", "requirePartitionFilter") if key in payload},
                }
                budget.consume_json(observations)
                assets.append(asset(source_id, "bigquery", [project, dataset], name, schema, observations))
            result = discovery(config, "bigquery", source_id, [project, dataset], assets)
            budget.consume_json(result)
            return result
    except ConnectorError:
        raise
    except Exception as exc:
        # Credentials, upstream error bodies and source account IDs stay out of
        # persisted failure snapshots and consumer tool responses.
        raise ConnectorError("sql_metadata_unavailable", "BigQuery metadata discovery failed") from exc
