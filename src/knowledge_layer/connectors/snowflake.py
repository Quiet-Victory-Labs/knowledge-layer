"""Optional Snowflake SDK discovery limited to one INFORMATION_SCHEMA scope."""

from __future__ import annotations

from collections import defaultdict
from contextlib import suppress
from typing import Any

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

_SCOPE = r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*"


def _credentials(profile: dict[str, Any]) -> dict[str, Any]:
    allowed = {"account", "user", "password", "token", "private_key_pem", "private_key_password", "role", "warehouse"}
    if not set(profile) <= allowed or not {"account", "user"} <= set(profile):
        raise ConnectorError("database_secret_invalid", "The Snowflake credential profile has invalid or unsupported fields")
    for key, value in profile.items():
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 32_768:
            raise ConnectorError("database_secret_invalid", "The Snowflake credential profile has invalid values")
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", profile["account"]):
        raise ConnectorError("database_secret_invalid", "The Snowflake account identifier is invalid")
    modes = [key for key in ("password", "token", "private_key_pem") if key in profile]
    if len(modes) != 1 or ("private_key_password" in profile and "private_key_pem" not in profile):
        raise ConnectorError("database_secret_invalid", "Configure exactly one Snowflake password, OAuth token or private key")
    result = {key: profile[key] for key in ("account", "user", "role", "warehouse") if key in profile}
    if modes[0] == "password":
        result.update(password=profile["password"], authenticator="snowflake")
    elif modes[0] == "token":
        result.update(token=profile["token"], authenticator="oauth")
    else:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

        try:
            key = serialization.load_pem_private_key(profile["private_key_pem"].encode(),
                password=profile["private_key_password"].encode() if "private_key_password" in profile else None)
            if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
                raise ValueError("Invalid key")
            result.update(authenticator="SNOWFLAKE_JWT", private_key=key.private_bytes(
                serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        except Exception as exc:
            raise ConnectorError("database_secret_invalid", "The Snowflake private key is invalid") from exc
    return result


def _query(connection: Any, statement: str, parameters: tuple[Any, ...], maximum: int, budget: MetadataBudget) -> list[tuple[Any, ...]]:
    cursor = connection.cursor()
    try:
        cursor.execute(statement, parameters, timeout=max(1, int(budget.timeout())))
        rows = cursor.fetchmany(maximum + 1)
        budget.timeout()
        if len(rows) > maximum:
            raise ConnectorError("table_limit_exceeded" if ".TABLES " in statement else "column_limit_exceeded",
                                 "The selected Snowflake scope exceeds its metadata budget")
        return rows
    finally:
        cursor.close()


def discover(config: dict[str, Any]) -> dict[str, Any]:
    reference, scope, selected, max_tables, max_columns = scope_config(config, "snowflake", _SCOPE)
    database, schema_name = scope.split(".")
    credentials = _credentials(secret_object(reference, "snowflake"))
    budget = MetadataBudget()
    source_id = config.get("source_id") or stable_id("source", "sql_catalog", "snowflake", scope, config.get("name"))
    connection = None
    try:
        try:
            import snowflake.connector
        except ImportError as exc:
            raise ConnectorError("sql_driver_unavailable", "Install the snowflake optional extra for native metadata discovery") from exc
        connection = snowflake.connector.connect(
            **credentials, database=database, schema=schema_name,
            login_timeout=CONNECT_TIMEOUT, network_timeout=budget.timeout(), socket_timeout=CONNECT_TIMEOUT,
            client_session_keep_alive=False, server_session_keep_alive=False,
            client_fetch_use_mp=False, client_prefetch_threads=1, autocommit=False,
            session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 10, "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": 5,
                                "CLIENT_SESSION_KEEP_ALIVE": False, "QUERY_TAG": "knowledge-layer:metadata-only"},
        )
        schemas = _query(connection,
            f'SELECT SCHEMA_NAME FROM "{database}".INFORMATION_SCHEMA.SCHEMATA '
            "WHERE SCHEMA_NAME = %s LIMIT 2", (schema_name,), 1, budget)
        if schemas != [(schema_name,)] and schemas != [[schema_name]]:
            raise ConnectorError("cloud_scope_unavailable", "The selected Snowflake schema is unavailable to this credential")
        where = "TABLE_SCHEMA = %s"
        params: tuple[Any, ...] = (schema_name,)
        if selected is not None:
            where += " AND TABLE_NAME IN (" + ",".join("%s" for _ in selected) + ")"
            params += tuple(selected)
        tables = _query(connection,
            f'SELECT TABLE_NAME, TABLE_TYPE, COMMENT, CREATED FROM "{database}".INFORMATION_SCHEMA.TABLES '
            f"WHERE {where} ORDER BY TABLE_NAME LIMIT {max_tables + 1}", params, max_tables, budget)
        names = []
        metadata = {}
        for row in tables:
            if not isinstance(row, (tuple, list)) or len(row) != 4:
                raise ConnectorError("cloud_metadata_invalid", "Snowflake returned invalid table metadata")
            name = text(row[0], maximum=1024)
            if name in metadata or (selected is not None and name not in selected):
                raise ConnectorError("cloud_metadata_invalid", "Snowflake returned an invalid selected scope")
            native = {"native_table_type": text(row[1], maximum=128), "source_description": text(row[2], optional=True),
                      "native_created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else text(row[3], maximum=128, optional=True)}
            budget.consume_json([name, native])
            names.append(name)
            metadata[name] = native
        if selected is not None and set(names) != set(selected):
            raise ConnectorError("table_not_found", "One or more selected Snowflake tables are unavailable")
        fields: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if names:
            placeholders = ",".join("%s" for _ in names)
            columns = _query(connection,
                f'SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, IS_NULLABLE, COMMENT, NUMERIC_PRECISION, NUMERIC_SCALE '
                f'FROM "{database}".INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s '
                f"AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME, ORDINAL_POSITION LIMIT {max_columns + 1}",
                (schema_name, *names), max_columns, budget)
            for row in columns:
                if not isinstance(row, (tuple, list)) or len(row) != 8 or row[0] not in metadata:
                    raise ConnectorError("cloud_metadata_invalid", "Snowflake returned invalid column metadata")
                if row[4] not in {"YES", "NO"} or type(row[2]) is not int or row[2] < 1:
                    raise ConnectorError("cloud_metadata_invalid", "Snowflake returned invalid column metadata")
                native_type = text(row[3], maximum=256)
                if any(value is not None and (type(value) is not int or value < 0 or value > 1000) for value in row[6:8]):
                    raise ConnectorError("cloud_metadata_invalid", "Snowflake returned invalid numeric metadata")
                if row[6] is not None and row[7] is not None:
                    native_type += f"({row[6]},{row[7]})"
                normalized_type = ("structured" if row[3] in {"VARIANT", "OBJECT", "ARRAY"}
                                   else "timestamp" if str(row[3]).startswith("TIMESTAMP_") else type_name(native_type))
                field = {"name": text(row[1], maximum=1024), "data_type": normalized_type,
                         "native_type": native_type, "nullable": row[4] == "YES",
                         "source_description": text(row[5], optional=True), "ordinal_position": row[2]}
                if any(item["name"] == field["name"] or item["ordinal_position"] == field["ordinal_position"] for item in fields[row[0]]):
                    raise ConnectorError("cloud_metadata_invalid", "Snowflake returned duplicate field metadata")
                budget.consume_json(field)
                fields[row[0]].append(field)
        assets = []
        for name in sorted(names):
            if not fields[name]:
                raise ConnectorError("cloud_metadata_invalid", "A selected Snowflake table has no visible schema")
            observations = {**metadata[name], "native_schema": fields[name], "application_rows_read": False}
            assets.append(asset(source_id, "snowflake", [credentials["account"], database, schema_name], name, fields[name], observations))
        result = discovery(config, "snowflake", source_id, [credentials["account"], database, schema_name], assets)
        budget.consume_json(result)
        return result
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError("sql_metadata_unavailable", "Snowflake metadata discovery failed") from exc
    finally:
        if connection is not None:
            # Preserve the safe primary failure if the SDK also fails cleanup.
            with suppress(Exception):
                connection.close()
