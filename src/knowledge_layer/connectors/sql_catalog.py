"""Bounded, metadata-only SQLAlchemy reflection for optional SQL dialects.

The connection URL lives only in a server-provisioned secret. This adapter does
not execute user SQL or read application rows. A dialect still needs its own
qualification before being described as production supported.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoSuchModuleError

from knowledge_layer.connectors.common import (
    ConnectorError,
    safe_identifier,
    stable_id,
    type_name,
)
from knowledge_layer.connectors.secrets import read_source_secret

PROVIDERS = {
    "postgresql", "mysql", "mariadb", "mssql", "oracle", "redshift",
    "snowflake", "bigquery", "databricks", "trino", "clickhouse", "duckdb", "sqlite",
}
MAX_TABLES = 100
MAX_COLUMNS = 5_000
CONNECT_TIMEOUT = 5
QUERY_TIMEOUT = 10
_BOUNDED_PROVIDERS = {"postgresql", "sqlite", "clickhouse"}
_NATIVE_METADATA_PROVIDERS = {"snowflake", "bigquery"}


def _engine_options(provider: str, url: Any) -> tuple[Any, dict[str, Any]]:
    """Fail closed for dialects without a tested read-only/time-budget policy."""
    if provider not in _BOUNDED_PROVIDERS:
        raise ConnectorError("sql_policy_unavailable", "This SQL dialect needs a qualified read-only timeout policy; use an external adapter")
    if provider == "postgresql":
        options = "-c default_transaction_read_only=on"
        if not (url.host and "-pooler" in url.host):
            options += f" -c statement_timeout={QUERY_TIMEOUT * 1000}"
        connect_args = {"connect_timeout": CONNECT_TIMEOUT, "options": options}
    elif provider == "clickhouse":
        if url.drivername != "clickhousedb":
            raise ConnectorError("sql_driver_unqualified", "Use the qualified clickhousedb dialect for ClickHouse metadata")
        connect_args = {"connect_timeout": CONNECT_TIMEOUT, "send_receive_timeout": QUERY_TIMEOUT + 5,
                        "query_retries": 0, "show_clickhouse_errors": False,
                        "settings": {"readonly": 1, "max_execution_time": QUERY_TIMEOUT,
                                     "max_result_rows": MAX_COLUMNS + 1, "max_result_bytes": 4 * 1024 * 1024,
                                     "result_overflow_mode": "throw"}}
    else:
        connect_args = {"timeout": CONNECT_TIMEOUT}
        if url.database in {None, "", ":memory:", "file::memory:"} or url.query.get("mode") == "memory":
            raise ConnectorError("sql_source_unavailable", "SQLite metadata requires an existing file source")
        database = url.database if url.database.startswith("file:") else f"file:{url.database}"
        url = url.set(database=database, query={**url.query, "mode": "ro", "uri": "true"})
    return url, {"connect_args": connect_args, "pool_pre_ping": False, "pool_size": 1,
                 "max_overflow": 0, "pool_timeout": CONNECT_TIMEOUT}


def _apply_clickhouse_driver_patches() -> None:
    try:
        import clickhouse_connect.driver as ch_driver
        from clickhouse_connect.driver.exceptions import ProgrammingError

        if getattr(ch_driver, "_kl_patched", False):
            return
        orig_create_client = ch_driver.create_client

        def safe_create_client(*args: Any, **kwargs: Any) -> Any:
            generic = kwargs.get("generic_args")
            if generic and "settings" in generic:
                s = generic.pop("settings")
                if not kwargs.get("settings"):
                    kwargs["settings"] = s
            return orig_create_client(*args, **kwargs)

        ch_driver.create_client = safe_create_client

        orig_validate = ch_driver.client.Client._validate_setting

        def safe_validate_setting(self: Any, key: str, value: Any, invalid_action: Any) -> Any:
            try:
                return orig_validate(self, key, value, invalid_action)
            except ProgrammingError as e:
                if "is readonly" in str(e):
                    return None
                raise

        ch_driver.client.Client._validate_setting = safe_validate_setting
        ch_driver._kl_patched = True
    except (ImportError, AttributeError):
        pass


def _read_only_session(connection: Any, provider: str) -> None:
    if provider == "postgresql":
        connection.exec_driver_sql(f"SET statement_timeout = {QUERY_TIMEOUT * 1000}")
        connection.exec_driver_sql("SET default_transaction_read_only = on")
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        if connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() != "on":
            raise ConnectorError("sql_readonly_unverified", "The source did not enforce a read-only transaction")
    elif provider == "sqlite":
        connection.exec_driver_sql("PRAGMA query_only = ON")
        if connection.exec_driver_sql("PRAGMA query_only").scalar_one() != 1:
            raise ConnectorError("sql_readonly_unverified", "The SQLite source did not enforce query-only mode")
        # The SQLite VM callback bounds actual reflection execution, independently
        # of its connection busy-lock timeout. It is scoped to this connection.
        import time

        deadline = time.monotonic() + QUERY_TIMEOUT
        connection.connection.driver_connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1_000)
    elif provider == "clickhouse":
        readonly, timeout = connection.exec_driver_sql(
            "SELECT getSetting('readonly'), getSetting('max_execution_time')"
        ).one()
        max_timeout = getattr(connection, "max_query_timeout", QUERY_TIMEOUT)
        if readonly not in (1, 2) or not 0 < timeout <= max_timeout:
            raise ConnectorError("sql_readonly_unverified", "ClickHouse did not enforce the required read-only query budget")


def _url(secret_ref: str, provider: str):
    value = read_source_secret(secret_ref, unavailable_code="database_credentials_unavailable")
    try:
        url = make_url(value)
    except Exception as exc:
        raise ConnectorError("database_secret_invalid", "The configured SQL secret is not a valid connection URL") from exc
    accepted_backends = {"clickhouse", "clickhousedb"} if provider == "clickhouse" else {provider}
    if url.get_backend_name() not in accepted_backends:
        raise ConnectorError("database_provider_mismatch", "The configured SQL secret does not match the selected provider")
    # The core image ships psycopg rather than psycopg2. Keep this choice on the
    # server, never in the customer-facing connection selector.
    if provider == "postgresql" and url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url


def discover(config: dict[str, Any]) -> dict[str, Any]:
    provider = config.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ConnectorError("invalid_sql_provider", "The selected SQL provider is unavailable")
    secret_ref = config.get("secret_ref")
    if not isinstance(secret_ref, str) or not re.fullmatch(r"KNOWLEDGE_SOURCE_[A-Z0-9_]{1,64}", secret_ref):
        raise ConnectorError("invalid_secret_reference", "A named server-side source reference is required")
    schema_name = config.get("schema")
    if schema_name is not None and not safe_identifier(schema_name):
        raise ConnectorError("invalid_schema_selector", "schema must be a safe identifier")
    selected = config.get("tables")
    if selected is not None and (
        not isinstance(selected, list) or not selected or len(selected) > MAX_TABLES
        or any(not safe_identifier(item) for item in selected)
    ):
        raise ConnectorError("invalid_table_selector", "tables must be a list of at most 100 safe names")
    selected_set = set(selected) if selected else None
    max_tables = config.get("max_tables", MAX_TABLES)
    max_columns = config.get("max_columns", MAX_COLUMNS)
    if type(max_tables) is not int or not 1 <= max_tables <= MAX_TABLES:
        raise ConnectorError("invalid_scan_limit", "max_tables must be between 1 and 100")
    if type(max_columns) is not int or not 1 <= max_columns <= MAX_COLUMNS:
        raise ConnectorError("invalid_scan_limit", "max_columns must be between 1 and 5000")

    # Native cloud metadata paths have fixed provider APIs, explicit qualified
    # scopes and their own request budgets; they do not use generic reflection.
    if schema_name is not None and provider in _NATIVE_METADATA_PROVIDERS:
        if provider == "snowflake":
            from knowledge_layer.connectors.snowflake import discover as native_discover
        else:
            from knowledge_layer.connectors.bigquery import discover as native_discover
        return native_discover(config)

    if provider == "clickhouse":
        _apply_clickhouse_driver_patches()
    url, engine_options = _engine_options(provider, _url(secret_ref, provider))
    source_id = config.get("source_id") or stable_id("source", "sql_catalog", provider, schema_name, config.get("name"))
    name = config.get("name") if isinstance(config.get("name"), str) else f"{provider} source"
    engine = None
    try:
        engine = create_engine(url, **engine_options)
        with engine.connect() as connection:
            if provider == "clickhouse" and hasattr(url, "query") and url.query and "max_execution_time" in url.query:
                try:
                    connection.max_query_timeout = float(url.query["max_execution_time"])
                except (ValueError, TypeError):
                    pass
            _read_only_session(connection, provider)
            inspector = inspect(connection)
            names = sorted(set(inspector.get_table_names(schema=schema_name)))
            if selected_set is not None:
                missing = selected_set - set(names)
                if missing:
                    raise ConnectorError("table_not_found", "One or more selected SQL tables were not found")
                names = [item for item in names if item in selected_set]
            if len(names) > max_tables:
                raise ConnectorError("table_limit_exceeded", "The selected SQL scope exceeds max_tables")
            row_estimates: dict[str, int | None] = {}
            if provider == "clickhouse":
                try:
                    ch_rows = connection.exec_driver_sql(
                        "SELECT name, total_rows FROM system.tables WHERE database = %(schema)s",
                        {"schema": schema_name or "default"},
                    ).all()
                    row_estimates = {
                        str(r[0]): int(r[1]) if r[1] is not None and int(r[1]) >= 0 else None
                        for r in ch_rows
                    }
                except Exception:  # noqa: BLE001, S110
                    pass
            assets = []
            column_count = 0
            for table_name in names:
                columns = inspector.get_columns(table_name, schema=schema_name)
                column_count += len(columns)
                if column_count > max_columns:
                    raise ConnectorError("column_limit_exceeded", "The selected SQL scope exceeds max_columns")
                schema = [
                    {
                        "name": str(column["name"]),
                        "data_type": type_name(column.get("type", "unknown")),
                        "nullable": column.get("nullable"),
                    }
                    for column in columns
                ]
                physical_schema = [
                    {
                        "name": str(column["name"]),
                        "data_type": (
                            column["type"].compile(dialect=engine.dialect)
                            if hasattr(column.get("type"), "compile")
                            else str(column["type"]) if column.get("type") is not None else None
                        ),
                        "nullable": column.get("nullable"),
                    }
                    for column in columns
                ]
                for field, physical_field in zip(schema, physical_schema, strict=True):
                    field["native_type"] = physical_field["data_type"]
                primary_key_state = "observed_metadata"
                try:
                    primary_key = [str(item) for item in (inspector.get_pk_constraint(table_name, schema=schema_name).get("constrained_columns") or [])]
                except (NotImplementedError, AttributeError):
                    primary_key = []
                    primary_key_state = "unknown"
                foreign_key_state = "observed_metadata"
                try:
                    foreign_keys = [
                        {
                            "columns": [str(value) for value in (foreign_key.get("constrained_columns") or [])],
                            "referred_table": str(foreign_key["referred_table"]),
                            "referred_schema": str(foreign_key.get("referred_schema") or schema_name or "default"),
                            "referred_columns": [str(value) for value in (foreign_key.get("referred_columns") or [])],
                        }
                        for foreign_key in inspector.get_foreign_keys(table_name, schema=schema_name)
                        if foreign_key.get("referred_table")
                    ]
                except (NotImplementedError, AttributeError):
                    foreign_keys = []
                    foreign_key_state = "unknown"
                pointer = f"sql/{provider}/{schema_name or 'default'}/{table_name}"
                assets.append({
                    "id": stable_id("asset", source_id, pointer),
                    "name": table_name,
                    "asset_type": "table",
                    "source_pointer": pointer,
                    "revision": stable_id("schema", physical_schema,
                                          primary_key if primary_key_state == "observed_metadata" else None,
                                          foreign_keys if foreign_key_state == "observed_metadata" else None),
                    "revision_basis": "schema_fingerprint",
                    "schema": schema,
                    "profile": {
                        "row_count": None, "row_count_is_complete": False,
                        "row_count_estimate": row_estimates.get(table_name),
                        "row_count_estimate_state": (
                            "estimated" if row_estimates.get(table_name) is not None else "unknown"
                        ),
                        "profile_rows": 0, "profile_is_sampled": False,
                        "null_counts": {}, "distinct_counts": {},
                        "metadata_only": True, "primary_key_columns": primary_key,
                        "primary_key_state": primary_key_state, "foreign_key_state": foreign_key_state,
                        **({"primary_key": primary_key} if primary_key_state == "observed_metadata" else {}),
                        **({"foreign_keys": foreign_keys} if foreign_key_state == "observed_metadata" else {}),
                    },
                    "trust_state": "unverified",
                })
    except ConnectorError:
        raise
    except (NoSuchModuleError, ImportError, ModuleNotFoundError) as exc:
        raise ConnectorError("sql_driver_unavailable", "The selected SQL provider driver is not installed") from exc
    except Exception as exc:
        # Driver exceptions may contain credentials, SQL, hostnames, or account IDs.
        raise ConnectorError("sql_metadata_unavailable", "SQL metadata discovery failed") from exc
    finally:
        if engine is not None:
            engine.dispose()

    return {
        "source": {
            "id": source_id, "name": name, "kind": "sql_catalog",
            "location": f"sql/{provider}/{schema_name or 'default'}",
            "revision": stable_id("revision", *[(item["source_pointer"], item["revision"]) for item in assets]),
            "trust_state": "unverified",
        },
        "assets": assets,
        "capabilities": {
            "metadata": True, "safe_profiling": False, "documents": False,
            "change_signals": True, "query_execution": False, "permission_mirroring": False,
        },
        "errors": [],
    }
