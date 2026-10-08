"""Bounded, metadata-only discovery from operator-provisioned Iceberg REST catalogs."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchTableError

from knowledge_layer.connectors.common import (
    ConnectorError,
    safe_identifier,
    stable_id,
    type_name,
)
from knowledge_layer.connectors.secrets import read_source_secret
from knowledge_layer.operator_secrets import SecretFileError, operator_secret, read_owner_only_file

MAX_TABLES = 100
MAX_COLUMNS = 5_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SOURCE_REFERENCE = re.compile(r"KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}\Z")


def _catalog_endpoint(value: Any) -> str:
    try:
        parsed = urlparse(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise ConnectorError("catalog_profile_invalid", "The configured catalog endpoint is invalid") from exc
    if parsed is None or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConnectorError("catalog_profile_invalid", "The configured catalog endpoint is invalid")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and (loopback or os.getenv("KNOWLEDGE_ALLOW_INSECURE_CATALOG_ENDPOINTS") == "1")
    ):
        raise ConnectorError("catalog_tls_required", "The catalog endpoint must use HTTPS or approved loopback HTTP")
    return value.rstrip("/")


def _bound_session(session: Any, approved_origins: set[tuple[str, str]]) -> Any:
    """Bound actual SDK requests, including the bootstrap legacy OAuth session.

    PyIceberg 0.12 does not implement the newer rest.client.* timeout properties.
    Wrapping its own session preserves its auth/REST implementation without a
    second catalog client. No process-wide requests state is changed.
    """
    if getattr(session, "_knowledge_bounded", False):
        return session
    original_request = session.request

    def request(method: str, url: str, **kwargs: Any) -> Any:
        _catalog_endpoint(url)
        parsed = urlparse(url)
        if (parsed.scheme, parsed.netloc) not in approved_origins:
            raise ConnectorError("catalog_endpoint_denied", "The catalog attempted an unapproved outbound origin")
        kwargs.update(timeout=(5, 15), allow_redirects=False, stream=True)
        response = original_request(method, url, **kwargs)
        try:
            chunks = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ConnectorError("catalog_response_too_large", "Catalog metadata exceeds the response budget")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and "identifiers" in payload:
                identifiers = payload["identifiers"]
                if not isinstance(identifiers, list) or len(identifiers) > MAX_TABLES or payload.get("next-page-token"):
                    raise ConnectorError("table_limit_exceeded", "Select explicit tables for a paginated or excessive catalog scope")
            return response
        finally:
            response.close()

    session.request = request
    session._knowledge_bounded = True
    return session


class _BoundedRestCatalog(RestCatalog):
    """The existing SDK catalog with request bounds, not a separate catalog engine."""

    def __init__(self, name: str, **properties: Any):
        endpoints = [properties["uri"]]
        if "oauth2-server-uri" in properties:
            endpoints.append(properties["oauth2-server-uri"])
        self._approved_origins = {(urlparse(url).scheme, urlparse(url).netloc) for url in endpoints}
        super().__init__(name, **properties)

    def _create_session(self) -> Any:
        return _bound_session(super()._create_session(), self._approved_origins)

    def _create_legacy_oauth2_auth_manager(self, session: Any) -> Any:
        return super()._create_legacy_oauth2_auth_manager(_bound_session(session, self._approved_origins))

    def _fetch_config(self) -> None:
        operator_properties = dict(self.properties)
        super()._fetch_config()
        # Remote defaults/overrides must not select local auth plugins, disable
        # TLS verification, or send credentials to an independently chosen host.
        if "auth" in self.properties or self.properties.get("rest.sigv4-enabled"):
            raise ConnectorError("catalog_profile_invalid", "Remote catalog authentication overrides are unsupported")
        if self.properties.get("ssl") != operator_properties.get("ssl"):
            raise ConnectorError("catalog_profile_invalid", "Remote catalog TLS overrides are unsupported")
        for key in ("uri", "oauth2-server-uri"):
            if key in self.properties:
                parsed = urlparse(_catalog_endpoint(self.properties[key]))
                if (parsed.scheme, parsed.netloc) not in self._approved_origins:
                    raise ConnectorError("catalog_endpoint_denied", "The catalog attempted an unapproved outbound origin")

    def _load_file_io(self, properties: Any = None, location: str | None = None) -> Any:
        # Table metadata is in the REST response. Avoid loading remotely selected
        # IO plugins or using vended object-storage credentials for this census.
        from pyiceberg.io.pyarrow import PyArrowFileIO

        return PyArrowFileIO({})


def catalog_profile_reference(catalog_name: str) -> str | None:
    """Validate the complete operator map before selecting one credential."""
    try:
        raw = operator_secret("KNOWLEDGE_ICEBERG_PROFILES")
    except ValueError as exc:
        raise ConnectorError("catalog_profile_invalid", "The operator catalog profile map is unavailable") from exc
    if raw is None:
        return None
    try:
        profiles = json.loads(raw)
    except ValueError as exc:
        raise ConnectorError("catalog_profile_invalid", "The operator catalog profile map is invalid") from exc
    if not isinstance(profiles, dict) or len(profiles) > 100 or any(
        not safe_identifier(name) or not isinstance(reference, str) or not _SOURCE_REFERENCE.fullmatch(reference)
        for name, reference in profiles.items()
    ):
        raise ConnectorError("catalog_profile_invalid", "The operator catalog profile map is invalid")
    reference = profiles.get(catalog_name)
    if reference is None:
        raise ConnectorError("catalog_not_allowlisted", "The catalog alias is not operator-provisioned")
    return reference


def _catalog_properties(catalog_name: str) -> dict[str, Any] | None:
    reference = catalog_profile_reference(catalog_name)
    if reference is None:
        return None
    try:
        properties = json.loads(read_source_secret(reference, unavailable_code="catalog_credentials_unavailable"))
    except ValueError as exc:
        raise ConnectorError("catalog_profile_invalid", "The configured catalog profile is invalid") from exc
    allowed = {"uri", "warehouse", "token", "credential", "oauth2-server-uri", "scope", "resource", "audience", "ssl",
               "s3.endpoint", "s3.region", "s3.access-key-id", "s3.secret-access-key", "s3.session-token", "s3.path-style-access"}
    if not isinstance(properties, dict) or not set(properties) <= allowed or "uri" not in properties:
        raise ConnectorError("catalog_profile_invalid", "The configured REST catalog profile is invalid or unsupported")
    if any(not isinstance(value, str) or not value for key, value in properties.items() if key != "ssl"):
        raise ConnectorError("catalog_profile_invalid", "Catalog profile properties must be nonempty strings")
    if "ssl" in properties:
        ssl = properties["ssl"]
        if not isinstance(ssl, dict) or not set(ssl) <= {"cabundle", "client"}:
            raise ConnectorError("catalog_profile_invalid", "The catalog TLS profile is invalid")
        if "cabundle" in ssl and ssl["cabundle"] is not True and not (
            isinstance(ssl["cabundle"], str) and ssl["cabundle"].strip()
        ):
            raise ConnectorError("catalog_profile_invalid", "The catalog TLS profile is invalid")
        if "client" in ssl and (not isinstance(ssl["client"], dict) or not ssl["client"]
                                 or not set(ssl["client"]) <= {"cert", "key"}
                                 or "cert" not in ssl["client"]
                                 or any(not isinstance(value, str) or not value.strip() for value in ssl["client"].values())):
            raise ConnectorError("catalog_profile_invalid", "The catalog TLS client profile is invalid")
    properties["uri"] = _catalog_endpoint(properties["uri"])
    if "oauth2-server-uri" in properties:
        _catalog_endpoint(properties["oauth2-server-uri"])
    if "s3.endpoint" in properties:
        _catalog_endpoint(properties["s3.endpoint"])
    properties["rest-page-size"] = str(MAX_TABLES + 1)
    return properties


def _client_credentials() -> tuple[str, str]:
    configured_file = os.environ.get("POLARIS_CLIENT_FILE", "").strip()
    if not configured_file:
        raise ConnectorError("catalog_credentials_unavailable", "POLARIS_CLIENT_FILE is not configured")
    client_file = Path(configured_file)
    if not client_file.is_absolute():
        raise ConnectorError("catalog_credentials_unavailable", "POLARIS_CLIENT_FILE must be an absolute path")
    try:
        payload = json.loads(read_owner_only_file(str(client_file)))
        client_id = payload["client_id"]
        client_secret = payload["client_secret"]
    except (OSError, UnicodeError, SecretFileError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConnectorError("catalog_credentials_unavailable", "Polaris credentials are not configured") from exc
    if not isinstance(client_id, str) or not isinstance(client_secret, str) or not client_id or not client_secret:
        raise ConnectorError("catalog_credentials_invalid", "Polaris credentials are not configured")
    return client_id, client_secret


def polaris_profile(
    uri: str,
    *,
    client_id: str,
    client_secret: str,
    warehouse: str,
    oauth2_server_uri: str | None = None,
    scope: str = "PRINCIPAL_ROLE:ALL",
) -> dict[str, Any]:
    """Construct a validated operator profile for Snowflake Apache Polaris Catalog."""
    oauth_uri = oauth2_server_uri or (uri.rstrip("/") + "/v1/oauth/tokens")
    return {
        "uri": uri,
        "warehouse": warehouse,
        "credential": f"{client_id}:{client_secret}",
        "oauth2-server-uri": oauth_uri,
        "scope": scope,
    }


def lakekeeper_profile(
    uri: str,
    *,
    warehouse: str,
    token: str | None = None,
) -> dict[str, Any]:
    """Construct an operator profile for Lakekeeper open-source Iceberg REST catalog."""
    profile: dict[str, Any] = {"uri": uri, "warehouse": warehouse}
    if token:
        profile["token"] = token
    return profile


def unity_catalog_profile(
    workspace_url: str,
    *,
    token: str,
    catalog_name: str,
) -> dict[str, Any]:
    """Construct an operator profile for Databricks / open Unity Catalog Iceberg REST API."""
    base = workspace_url.rstrip("/")
    uri = base if "/api/2.1/unity-catalog/iceberg" in base else f"{base}/api/2.1/unity-catalog/iceberg"
    return {
        "uri": uri,
        "token": token,
        "warehouse": catalog_name,
    }


def nessie_profile(
    uri: str,
    *,
    warehouse: str = "main",
    token: str | None = None,
) -> dict[str, Any]:
    """Construct an operator profile for Project Nessie Iceberg REST Catalog."""
    profile: dict[str, Any] = {"uri": uri, "warehouse": warehouse}
    if token:
        profile["token"] = token
    return profile


def _table_names(catalog: Any, namespace: str, requested: Any) -> list[str]:
    if requested is not None:
        if not isinstance(requested, list) or not requested or len(requested) > MAX_TABLES or any(not safe_identifier(value) for value in requested):
            raise ConnectorError("invalid_table_selector", "tables must contain at most 100 safe selectors")
        # Explicit scopes load tables directly; listing a large namespace merely
        # to validate a narrow selector defeats its discovery budget.
        return sorted(set(requested))
    identifiers = catalog.list_tables(namespace)
    available = {
        identifier[-1] if isinstance(identifier, (tuple, list)) else str(identifier).split(".")[-1]
        for identifier in identifiers
    }
    names = sorted(available)
    if len(names) > MAX_TABLES:
        raise ConnectorError("table_limit_exceeded", f"The namespace exceeds the {MAX_TABLES}-table discovery limit")
    return names


def discover(config: dict[str, Any]) -> dict[str, Any]:
    catalog_name = config.get("catalog")
    namespace = config.get("namespace")
    if not safe_identifier(catalog_name) or not safe_identifier(namespace):
        raise ConnectorError("invalid_catalog_selector", "catalog and namespace must be valid identifiers")
    properties = _catalog_properties(str(catalog_name))
    if properties is not None:
        catalog = None
        try:
            catalog = _BoundedRestCatalog(str(catalog_name), **properties)
            return _discover_catalog(config, catalog)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError("catalog_unavailable", "REST catalog discovery failed") from exc
        finally:
            if session := getattr(catalog, "_session", None):
                session.close()
    allowlist = {
        value.strip()
        for value in os.environ.get("KNOWLEDGE_ICEBERG_CATALOGS", "").split(",")
        if value.strip()
    }
    if not allowlist:
        raise ConnectorError("catalog_allowlist_missing", "No Polaris catalogs are configured for this tenant")
    if catalog_name not in allowlist:
        raise ConnectorError("catalog_not_allowlisted", "The selected Polaris catalog is not configured for this tenant")
    catalog_url = os.environ.get("POLARIS_URL", "http://localhost:8181").rstrip("/")
    s3_endpoint = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        raise ConnectorError("catalog_storage_credentials_unavailable", "Polaris storage credentials are not configured")
    client_id, client_secret = _client_credentials()
    catalog = None
    try:
        catalog = _BoundedRestCatalog(
            str(catalog_name),
            type="rest",
            uri=f"{catalog_url}/api/catalog",
            warehouse=str(catalog_name),
            credential=f"{client_id}:{client_secret}",
            scope="PRINCIPAL_ROLE:ALL",
            **{
                "oauth2-server-uri": f"{catalog_url}/api/catalog/v1/oauth/tokens",
                "s3.endpoint": s3_endpoint,
                "s3.access-key-id": access_key,
                "s3.secret-access-key": secret_key,
                "s3.region": "us-east-1",
                "s3.path-style-access": "true",
                "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            },
        )
        return _discover_catalog(config, catalog)
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError("catalog_unavailable", "REST catalog discovery failed") from exc
    finally:
        if session := getattr(catalog, "_session", None):
            session.close()


def _discover_catalog(config: dict[str, Any], catalog: Any) -> dict[str, Any]:
    catalog_name = config["catalog"]
    namespace = config["namespace"]
    if not catalog.namespace_exists(str(namespace)):
        raise ConnectorError("namespace_not_found", "The selected namespace does not exist")
    names = _table_names(catalog, str(namespace), config.get("tables"))

    name = config.get("name") if isinstance(config.get("name"), str) else f"{catalog_name}.{namespace}"
    source_id = config.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        source_id = stable_id("source", "iceberg", catalog_name, namespace, name)
    assets = []
    column_count = 0
    for table_name in names:
        identifier = f"{namespace}.{table_name}"
        try:
            table = catalog.load_table(identifier)
            table_uuid = _source_table_uuid(table)
            current_schema = table.schema()
            column_count += len(current_schema.field_ids)
            if column_count > MAX_COLUMNS:
                raise ConnectorError("column_limit_exceeded", "The selected Iceberg scope exceeds the column budget")
            schema = [
                {
                    "name": field.name,
                    "data_type": type_name(field.field_type),
                    "native_type": str(field.field_type),
                    "field_id": field.field_id,
                    "nullable": not field.required,
                }
                for field in current_schema.fields
            ]
            snapshot = table.current_snapshot()
            snapshot_id = str(snapshot.snapshot_id) if snapshot else None
            if snapshot:
                summary_object = snapshot.summary
                summary = (
                    summary_object.model_dump()
                    if hasattr(summary_object, "model_dump")
                    else dict(summary_object)
                )
            else:
                summary = {}
            try:
                row_count = _optional_int(summary.get("total-records"))
            except (ValueError, TypeError):
                row_count = None
            source_pointer = f"{catalog_name}.{namespace}.{table_name}"
            assets.append(
                {
                    "id": stable_id("asset", source_id, catalog_name, namespace, table_name),
                    "name": table_name,
                    "asset_type": "table",
                    "source_pointer": source_pointer,
                    # Bind the table identity as well: a replacement at the same
                    # name can have an identical schema and data snapshot ID.
                    # Schemas also evolve independently of data snapshots.
                    "revision": stable_id(
                        "iceberg_metadata", table_uuid, snapshot_id, current_schema.model_dump(mode="json")
                    ),
                    "revision_basis": "iceberg_table_snapshot_and_schema" if snapshot_id else "iceberg_table_schema_fingerprint",
                    "schema": schema,
                    "profile": {
                        "row_count": row_count,
                        "row_count_basis": "snapshot_summary" if row_count is not None else "unknown",
                        "row_count_is_complete": row_count is not None,
                        "profile_rows": 0,
                        "profile_is_sampled": False,
                        "null_counts": {},
                        "distinct_counts": {},
                        "metadata_only": True,
                        "table_uuid": table_uuid,
                        "table_uuid_state": "observed_metadata" if table_uuid is not None else "unknown",
                        "schema_id": current_schema.schema_id,
                        "snapshot_id": _optional_int(snapshot.snapshot_id) if snapshot is not None else None,
                        "file_count": _optional_int(summary.get("total-data-files")),
                    },
                    "trust_state": "unverified",
                }
            )
        except NoSuchTableError as exc:
            raise ConnectorError("table_not_found", "One or more selected Iceberg tables were not found") from exc
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError("table_metadata_unavailable", "Iceberg table metadata could not be read", identifier) from exc

    aggregate_revision = stable_id("revision", *[(asset["source_pointer"], asset["revision"]) for asset in assets])
    return {
        "source": {"id": source_id, "name": name, "kind": "iceberg", "location": f"{catalog_name}.{namespace}", "revision": aggregate_revision, "trust_state": "unverified"},
        "assets": assets,
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": False, "change_signals": True, "query_execution": False, "permission_mirroring": False},
        "errors": [],
    }


def _source_table_uuid(table: Any) -> str | None:
    metadata = getattr(table, "metadata", None)
    if metadata is None:
        return None
    # PyIceberg generates uuid4 when the source omits table-uuid. Pydantic's
    # supplied-field set distinguishes that local default from an observed ID.
    supplied = getattr(metadata, "model_fields_set", None)
    if supplied is not None and "table_uuid" not in supplied:
        return None
    value = getattr(metadata, "table_uuid", None)
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConnectorError("table_metadata_unavailable", "Iceberg table identity is invalid") from exc


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        return None
    try:
        number = int(value) if value is not None else None
        return number if number is not None and number >= 0 else None
    except (ValueError, TypeError):
        return None
