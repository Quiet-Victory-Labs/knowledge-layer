"""Metadata-only discovery for PostgreSQL; no application tables are scanned."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from knowledge_layer.connectors.common import (
    ConnectorError,
    safe_identifier,
    stable_id,
    type_name,
)
from knowledge_layer.connectors.secrets import read_source_secret

MAX_TABLES = 100
MAX_COLUMNS = 5_000


def _secret_name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value):
        raise ConnectorError("invalid_secret_reference", "secret_ref must name a server-side uppercase environment variable")
    return value


def _selected_tables(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or len(value) > MAX_TABLES:
        raise ConnectorError("invalid_table_selector", "tables must be a non-empty list of at most 100 names")
    if any(not safe_identifier(name) for name in value):
        raise ConnectorError("invalid_table_selector", "tables contains an invalid selector")
    return sorted(set(value))


def discover(config: dict[str, Any]) -> dict[str, Any]:
    secret_name = _secret_name(config.get("secret_ref"))
    dsn = read_source_secret(secret_name, unavailable_code="database_credentials_unavailable")
    schema_name = config.get("schema", "public")
    if not safe_identifier(schema_name):
        raise ConnectorError("invalid_schema_selector", "schema must be a valid identifier")
    requested = _selected_tables(config.get("tables"))
    timeout = config.get("connect_timeout", 5)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 10:
        raise ConnectorError("invalid_timeout", "connect_timeout must be an integer between 1 and 10 seconds")

    try:
        import psycopg
    except ImportError as exc:
        raise ConnectorError("postgres_driver_unavailable", "The PostgreSQL connector dependency is unavailable") from exc

    columns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    physical_columns: dict[str, list[tuple[str, str | None, bool | None]]] = defaultdict(list)
    row_estimates: dict[str, int | None] = {}
    try:
        with psycopg.connect(
            dsn,
            connect_timeout=timeout,
            options="-c default_transaction_read_only=on",
        ) as connection, connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {timeout * 1000}")
            cursor.execute("SET default_transaction_read_only = on")
            cursor.execute("SET TRANSACTION READ ONLY")
            selector = ""
            params: list[Any] = [schema_name]
            if requested is not None:
                selector = " AND t.table_name = ANY(%s)"
                params.append(requested)
            cursor.execute(
                """
                SELECT t.table_name, c.column_name, c.data_type, c.is_nullable,
                       pg_catalog.format_type(a.atttypid, a.atttypmod)
                FROM information_schema.tables AS t
                JOIN information_schema.columns AS c
                  ON c.table_catalog = t.table_catalog
                 AND c.table_schema = t.table_schema
                 AND c.table_name = t.table_name
                JOIN pg_catalog.pg_class AS r ON r.relname = t.table_name
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = r.relnamespace AND n.nspname = t.table_schema
                JOIN pg_catalog.pg_attribute AS a
                  ON a.attrelid = r.oid AND a.attname = c.column_name
                 AND a.attnum > 0 AND NOT a.attisdropped
                WHERE t.table_schema = %s
                  AND t.table_type IN ('BASE TABLE', 'VIEW', 'FOREIGN')
                """ + selector + " ORDER BY t.table_name, c.ordinal_position LIMIT %s",
                [*params, MAX_COLUMNS + 1],
            )
            rows = cursor.fetchall()
            if len(rows) > MAX_COLUMNS:
                raise ConnectorError("column_limit_exceeded", "The selected PostgreSQL scope exceeds the metadata column limit")
            for table_name, column_name, data_type, is_nullable, physical_type in rows:
                nullable = {"yes": True, "no": False}.get(str(is_nullable).casefold())
                columns[str(table_name)].append(
                    {
                        "name": str(column_name),
                        "data_type": type_name(data_type),
                        "native_type": str(physical_type) if physical_type is not None else None,
                        "nullable": nullable,
                    }
                )
                physical_columns[str(table_name)].append(
                    (str(column_name), str(physical_type) if physical_type is not None else None, nullable)
                )
            if len(columns) > MAX_TABLES:
                raise ConnectorError("table_limit_exceeded", "The selected PostgreSQL scope exceeds the table limit")
            if requested is not None:
                missing = sorted(set(requested) - set(columns))
                if missing:
                    raise ConnectorError("table_not_found", "One or more selected PostgreSQL tables were not found")
            cursor.execute(
                """
                SELECT c.relname, c.reltuples
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
                  AND c.relname = ANY(%s)
                """,
                (schema_name, sorted(columns)),
            )
            # PostgreSQL uses negative reltuples when a relation has not been
            # analyzed. That is unknown, not evidence that the table is empty.
            row_estimates = {
                str(name): int(count) if count is not None and count >= 0 else None
                for name, count in cursor.fetchall()
            }
    except ConnectorError:
        raise
    except Exception as exc:
        # Never surface the DSN or driver diagnostics, which may contain credentials.
        raise ConnectorError("database_metadata_unavailable", "PostgreSQL metadata discovery failed") from exc

    name = config.get("name") if isinstance(config.get("name"), str) else "PostgreSQL source"
    source_id = config.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        source_id = stable_id("source", "postgres", schema_name, name)
    assets = []
    for table_name, schema in sorted(columns.items()):
        pointer = f"postgres/{schema_name}/{table_name}"
        assets.append(
            {
                "id": stable_id("asset", source_id, pointer),
                "name": table_name,
                "asset_type": "table",
                "source_pointer": pointer,
                # The display type is intentionally broad. Bind revisions to
                # native types so width, scale, precision and zone drift survive.
                "revision": stable_id("schema", physical_columns[table_name]),
                "revision_basis": "schema_fingerprint",
                "schema": schema,
                "profile": {
                    "row_count": None,
                    "row_count_is_complete": False,
                    "row_count_estimate": row_estimates.get(table_name),
                    "row_count_estimate_state": (
                        "estimated" if row_estimates.get(table_name) is not None else "unknown"
                    ),
                    "profile_rows": 0,
                    "profile_is_sampled": False,
                    "null_counts": {},
                    "distinct_counts": {},
                    "metadata_only": True,
                },
                "trust_state": "unverified",
            }
        )
    aggregate_revision = stable_id("revision", *[(asset["source_pointer"], asset["revision"]) for asset in assets])
    return {
        "source": {"id": source_id, "name": name, "kind": "postgres", "location": f"postgres/{schema_name}", "revision": aggregate_revision, "trust_state": "unverified"},
        "assets": assets,
        "capabilities": {"metadata": True, "safe_profiling": False, "documents": False, "change_signals": False, "query_execution": False, "permission_mirroring": False},
        "errors": [],
    }
