"""Exercise native connector policies through the installed SDK/SQL engine paths."""

from __future__ import annotations

import io
import json
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from knowledge_layer.connectors import postgres, sql_catalog
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.curation import iceberg


@pytest.fixture
def rest_profile(monkeypatch):
    if iceberg is None:
        pytest.skip("Install the iceberg extra for installed-SDK qualification")
    profile = {"uri": "http://localhost:8181/catalog", "warehouse": "warehouse-id"}
    monkeypatch.setenv("KNOWLEDGE_ICEBERG_PROFILES", json.dumps({"local_lake": "KNOWLEDGE_SOURCE_TEST_ICEBERG"}))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_ICEBERG", json.dumps(profile))
    return profile


def _table_response(count=None):
    from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC
    from pyiceberg.schema import Schema
    from pyiceberg.table.metadata import new_table_metadata
    from pyiceberg.table.sorting import UNSORTED_SORT_ORDER
    from pyiceberg.types import IntegerType, NestedField, StructType

    schema = Schema(
        NestedField(1, "id", IntegerType(), required=True),
        NestedField(2, "detail", StructType(NestedField(3, "count", IntegerType()))),
    )
    metadata = new_table_metadata(schema, UNPARTITIONED_PARTITION_SPEC, UNSORTED_SORT_ORDER,
                                  "s3://lake/service/events").model_dump(mode="json", by_alias=True)
    if count is not None:
        metadata.update({"current-snapshot-id": 42, "last-sequence-number": 1, "snapshots": [{
            "snapshot-id": 42, "sequence-number": 1, "timestamp-ms": 1,
            "manifest-list": "s3://lake/metadata/manifests.avro",
            "summary": {"operation": "append", "total-records": count, "total-data-files": "0"},
        }]})
    # The metadata-only connector must not load a remotely chosen local plugin.
    return {"metadata-location": "s3://lake/metadata/v1.json", "metadata": metadata,
            "config": {"py-io-impl": "untrusted.package.FileIO"}}


def _rest_transport(monkeypatch, *, table=None, config=None, listing=None):
    import requests

    calls = []

    def request(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        path = urlparse(url).path
        if path.endswith("/token"):
            payload = {"access_token": "private-bearer", "token_type": "Bearer", "expires_in": 3600}
        elif path.endswith("/config"):
            payload = config if config is not None else {"defaults": {}, "overrides": {"prefix": "warehouse-id"}}
        elif method == "HEAD":
            payload = b""
        elif path.endswith("/namespaces/service"):
            payload = {"namespace": ["service"], "properties": {}}
        elif path.endswith("/tables/events"):
            payload = table if table is not None else _table_response()
        elif path.endswith("/tables"):
            payload = listing if listing is not None else {"identifiers": [{"namespace": ["service"], "name": "events"}]}
        else:
            pytest.fail(f"Unexpected SDK request: {method} {path}")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response.raw = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return calls


@pytest.mark.parametrize(("count", "expected"), [(None, None), ("-1", None), ("0", 0), ("12", 12), ("1.5", None)])
def test_generic_rest_uses_installed_sdk_with_bounded_metadata_only_requests(monkeypatch, rest_profile, count, expected):
    calls = _rest_transport(monkeypatch, table=_table_response(count))
    result = iceberg.discover({"catalog": "local_lake", "namespace": "service", "tables": ["events"]})
    asset = result["assets"][0]
    assert asset["profile"]["row_count"] == expected
    assert asset["profile"]["row_count_basis"] == ("snapshot_summary" if expected is not None else "unknown")
    assert asset["profile"]["snapshot_id"] == (42 if count is not None else None)
    assert asset["profile"]["row_count_is_complete"] is (expected is not None)
    assert asset["profile"]["profile_rows"] == 0
    assert asset["schema"][0]["native_type"] == "int"
    assert asset["schema"][0]["field_id"] == 1
    assert asset["schema"][1]["data_type"] == "structured"
    assert len(calls) == 3
    assert all(kwargs["timeout"] == (5, 15) and kwargs["stream"] is True
               and kwargs["allow_redirects"] is False for _, _, kwargs in calls)
    assert not any(urlparse(url).path.endswith("/tables") for _, url, _ in calls)
    assert "private-bearer" not in json.dumps(result)
    assert "s3://" not in json.dumps(result)


def test_legacy_oauth_bootstrap_is_bounded_before_sdk_refresh(monkeypatch, rest_profile):
    rest_profile.update(credential="private-client:private-secret", **{"oauth2-server-uri": "http://localhost:8181/token"})
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_ICEBERG", json.dumps(rest_profile))
    calls = _rest_transport(monkeypatch)
    result = iceberg.discover({"catalog": "local_lake", "namespace": "service", "tables": ["events"]})
    tokens = [kwargs for method, url, kwargs in calls if method == "POST" and url.endswith("/token")]
    assert tokens
    assert all(kwargs["timeout"] == (5, 15) and kwargs["allow_redirects"] is False for kwargs in tokens)
    assert "private-secret" not in json.dumps(result)


@pytest.mark.parametrize("override", [
    {"uri": "https://other.example/catalog"},
    {"oauth2-server-uri": "https://other.example/token"},
    {"auth": {"type": "custom", "impl": "untrusted.package.Auth"}},
    {"ssl": {"cabundle": False}},
])
def test_rest_remote_overrides_cannot_choose_client_security_policy(monkeypatch, rest_profile, override):
    calls = _rest_transport(monkeypatch, config={"defaults": {}, "overrides": override})
    with pytest.raises(ConnectorError) as error:
        iceberg.discover({"catalog": "local_lake", "namespace": "service", "tables": ["events"]})
    assert error.value.code in {"catalog_profile_invalid", "catalog_endpoint_denied"}
    assert len(calls) == 1
    assert "other.example" not in str(error.value)
    assert "untrusted" not in str(error.value)


def test_rest_paginated_scope_refuses_instead_of_claiming_complete(monkeypatch, rest_profile):
    calls = _rest_transport(monkeypatch, listing={"identifiers": [{"namespace": ["service"], "name": "events"}],
                                                "next-page-token": "private-cursor"})
    with pytest.raises(ConnectorError) as error:
        iceberg.discover({"catalog": "local_lake", "namespace": "service"})
    assert error.value.code == "table_limit_exceeded"
    assert len(calls) == 3
    assert "private-cursor" not in str(error.value)


def test_rest_response_and_nested_field_budgets_are_enforced(monkeypatch, rest_profile):
    _rest_transport(monkeypatch)
    monkeypatch.setattr(iceberg, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(ConnectorError) as error:
        iceberg.discover({"catalog": "local_lake", "namespace": "service", "tables": ["events"]})
    assert error.value.code == "catalog_response_too_large"
    monkeypatch.setattr(iceberg, "MAX_RESPONSE_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(iceberg, "MAX_COLUMNS", 2)
    with pytest.raises(ConnectorError) as error:
        iceberg.discover({"catalog": "local_lake", "namespace": "service", "tables": ["events"]})
    assert error.value.code == "column_limit_exceeded"


def test_rest_table_replacement_invalidates_evidence_with_identical_schema_and_snapshot(monkeypatch, rest_profile):
    from knowledge_layer.curation import curate_source

    response = _table_response("0")
    first_uuid = response["metadata"]["table-uuid"]
    _rest_transport(monkeypatch, table=response)
    config = {"catalog": "local_lake", "namespace": "service", "tables": ["events"], "source_id": "stable-source"}
    first = curate_source("iceberg", config)
    unchanged = curate_source("iceberg", {**config, "previous_snapshot": first})
    assert unchanged["drift"]["changed_assets"] == []
    assert first["assets"][0]["profile"]["table_uuid"] == first_uuid
    response["metadata"]["table-uuid"] = "00000000-0000-0000-0000-000000000002"
    replacement = curate_source("iceberg", {**config, "previous_snapshot": first})
    assert replacement["discovery_complete"] is True
    assert replacement["assets"][0]["schema"] == first["assets"][0]["schema"]
    assert replacement["assets"][0]["id"] == first["assets"][0]["id"]
    assert replacement["assets"][0]["revision"] != first["assets"][0]["revision"]
    assert replacement["drift"]["changed_assets"][0]["change_types"] == ["revision_changed", "profile_changed"]
    assert {row["id"] for row in first["evidence"]}.isdisjoint({row["id"] for row in replacement["evidence"]})


def test_rest_missing_uuid_does_not_fingerprint_sdk_generated_identity(monkeypatch, rest_profile):
    from knowledge_layer.curation import curate_source

    response = _table_response("0")
    del response["metadata"]["table-uuid"]
    _rest_transport(monkeypatch, table=response)
    config = {"catalog": "local_lake", "namespace": "service", "tables": ["events"], "source_id": "stable-source"}
    first = curate_source("iceberg", config)
    unchanged = curate_source("iceberg", {**config, "previous_snapshot": first})
    assert first["discovery_complete"] is True
    assert first["assets"][0]["profile"]["table_uuid"] is None
    assert first["assets"][0]["profile"]["table_uuid_state"] == "unknown"
    assert unchanged["drift"]["changed_assets"] == []
    assert unchanged["snapshot_id"] == first["snapshot_id"]
    response["metadata"]["table-uuid"] = "00000000-0000-0000-0000-000000000001"
    measured = curate_source("iceberg", {**config, "previous_snapshot": first})
    assert measured["assets"][0]["profile"]["table_uuid_state"] == "observed_metadata"
    assert measured["assets"][0]["revision"] != first["assets"][0]["revision"]


@pytest.mark.parametrize("profile", [
    {"uri": "http://catalog.example"}, {"uri": "https://user:private@catalog.example"},  # pragma: allowlist secret - synthetic rejected credential fixture
    {"uri": "https://catalog.example", "ssl": {"cabundle": False}},
    {"uri": "https://catalog.example", "ssl": {"cabundle": ""}},
    {"uri": "https://catalog.example", "ssl": {"cabundle": None}},
    {"uri": "https://catalog.example", "auth": {"type": "custom"}},
])
def test_rest_unqualified_profiles_are_refused_before_connection(monkeypatch, rest_profile, profile):
    calls = _rest_transport(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_ICEBERG", json.dumps(profile))
    with pytest.raises(ConnectorError):
        iceberg.discover({"catalog": "local_lake", "namespace": "service"})
    assert calls == []


@pytest.mark.parametrize("provider", sorted(sql_catalog.PROVIDERS - {"postgresql", "sqlite", "clickhouse"}))
def test_unqualified_sql_dialects_fail_closed_before_engine_creation(provider, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_SQL", f"{provider}://source/example")
    monkeypatch.setattr(sql_catalog, "create_engine", lambda *_args, **_kwargs: pytest.fail("unqualified connection"))
    with pytest.raises(ConnectorError) as error:
        sql_catalog.discover({"provider": provider, "secret_ref": "KNOWLEDGE_SOURCE_TEST_SQL"})  # pragma: allowlist secret - operator reference
    assert error.value.code == "sql_policy_unavailable"


def test_qualified_sql_driver_bounds_and_server_readonly_verification():
    _, pg = sql_catalog._engine_options("postgresql", make_url("postgresql+psycopg://source/example"))
    assert pg["connect_args"] == {"connect_timeout": 5, "options": "-c default_transaction_read_only=on -c statement_timeout=10000"}
    _, ch = sql_catalog._engine_options("clickhouse", make_url("clickhousedb://source/example"))
    assert ch["connect_args"]["query_retries"] == 0
    assert ch["connect_args"]["connect_timeout"] == 5
    assert ch["connect_args"]["send_receive_timeout"] == 15
    assert ch["connect_args"]["settings"]["readonly"] == 1
    assert ch["connect_args"]["settings"]["max_execution_time"] == 10
    for readonly, timeout in [(0, 10), (1, 0), (1, 11)]:
        connection = SimpleNamespace(exec_driver_sql=lambda _sql, values=(readonly, timeout): SimpleNamespace(one=lambda: values))
        with pytest.raises(ConnectorError, match="required read-only"):
            sql_catalog._read_only_session(connection, "clickhouse")
    with pytest.raises(ConnectorError) as error:
        sql_catalog._engine_options("clickhouse", make_url("clickhouse://source/example"))
    assert error.value.code == "sql_driver_unqualified"


def test_sqlite_actual_readonly_effect_and_no_missing_file_creation(tmp_path):
    database = tmp_path / "existing.db"
    setup = create_engine(f"sqlite:///{database}")
    with setup.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE events (id INTEGER)")
    setup.dispose()
    url, options = sql_catalog._engine_options("sqlite", make_url(f"sqlite:///{database}"))
    engine = create_engine(url, **options)
    try:
        with engine.connect() as connection:
            sql_catalog._read_only_session(connection, "sqlite")
            with pytest.raises(OperationalError, match="readonly"):
                connection.exec_driver_sql("INSERT INTO events VALUES (1)")
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM events").scalar_one() == 0
    finally:
        engine.dispose()
    absent = tmp_path / "absent.db"
    url, options = sql_catalog._engine_options("sqlite", make_url(f"sqlite:///{absent}"))
    engine = create_engine(url, **options)
    try:
        with pytest.raises(OperationalError), engine.connect():
            pass
    finally:
        engine.dispose()
    assert not absent.exists()


def test_sqlite_vm_timeout_interrupts_actual_query(tmp_path, monkeypatch):
    import time

    database = tmp_path / "existing.db"
    database.touch()
    url, options = sql_catalog._engine_options("sqlite", make_url(f"sqlite:///{database}"))
    engine = create_engine(url, **options)
    ticks = iter([0, 11])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 11))
    try:
        with engine.connect() as connection:
            sql_catalog._read_only_session(connection, "sqlite")
            with pytest.raises(OperationalError, match="interrupted"):
                connection.exec_driver_sql("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n")
    finally:
        engine.dispose()


def test_postgres_metadata_nullability_unknown_and_selected_estimate_scope(monkeypatch):
    statements = []

    def execute(statement, params=None):
        statements.append((statement, params))

    def fetchall():
        if "information_schema" in statements[-1][0]:
            return [("events", "value", "USER-DEFINED", None, None)]
        return []

    cursor = SimpleNamespace(execute=execute, fetchall=fetchall)
    connection = SimpleNamespace(cursor=lambda: nullcontext(cursor))
    import sys

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *_args, **_kwargs: nullcontext(connection)))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_PG", "postgresql://source/example")
    result = postgres.discover({"secret_ref": "KNOWLEDGE_SOURCE_TEST_PG", "tables": ["events"]})  # pragma: allowlist secret - operator reference
    asset = result["assets"][0]
    assert asset["schema"][0]["nullable"] is None
    assert asset["schema"][0]["native_type"] is None
    assert asset["profile"]["row_count_estimate"] is None
    assert asset["profile"]["row_count_estimate_state"] == "unknown"
    assert "c.relname = ANY(%s)" in statements[-1][0]
    assert statements[-1][1] == ("public", ["events"])


def test_sql_reflection_preserves_unmeasured_native_type_and_nullability(monkeypatch):
    inspector = SimpleNamespace(
        get_table_names=lambda **_kwargs: ["events"],
        get_columns=lambda *_args, **_kwargs: [{"name": "unknown_field"}],
        get_pk_constraint=lambda *_args, **_kwargs: {},
        get_foreign_keys=lambda *_args, **_kwargs: [],
    )
    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(exec_driver_sql=lambda _sql: SimpleNamespace(scalar_one=lambda: "on"))),
        dispose=lambda: None, dialect=None,
    )
    monkeypatch.setattr(sql_catalog, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(sql_catalog, "inspect", lambda _connection: inspector)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_SQL", "postgresql://source/example")
    result = sql_catalog.discover({"provider": "postgresql", "secret_ref": "KNOWLEDGE_SOURCE_TEST_SQL"})  # pragma: allowlist secret - operator reference
    assert result["assets"][0]["schema"] == [{"name": "unknown_field", "data_type": "unknown", "native_type": None, "nullable": None}]
    assert result["assets"][0]["profile"]["row_count"] is None


@pytest.mark.parametrize("change", ["integer_width", "decimal_scale", "decimal_precision", "timezone"])
def test_clickhouse_installed_dialect_physical_drift(monkeypatch, change):
    pytest.importorskip("clickhouse_connect")
    from clickhouse_connect.cc_sqlalchemy.datatypes.sqltypes import (
        DateTime64,
        Decimal,
        UInt32,
        UInt64,
    )
    from clickhouse_connect.cc_sqlalchemy.dialect import ClickHouseDialect

    from knowledge_layer.curation import curate_source

    pairs = {
        "integer_width": (UInt32(), UInt64()),
        "decimal_scale": (Decimal(10, 2), Decimal(10, 3)),
        "decimal_precision": (Decimal(10, 2), Decimal(12, 2)),
        "timezone": (DateTime64(3, "UTC"), DateTime64(3, "America/Chicago")),
    }
    before, after = pairs[change]
    reflected = {"type": before}
    inspector = SimpleNamespace(
        get_table_names=lambda **_kwargs: ["events"],
        get_columns=lambda *_args, **_kwargs: [{"name": "value", "type": reflected["type"], "nullable": True}],
        get_pk_constraint=lambda *_args, **_kwargs: {},
        get_foreign_keys=lambda *_args, **_kwargs: [],
    )
    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(exec_driver_sql=lambda _sql: SimpleNamespace(one=lambda: (1, 10)))),
        dispose=lambda: None, dialect=ClickHouseDialect(),
    )
    monkeypatch.setattr(sql_catalog, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(sql_catalog, "inspect", lambda _connection: inspector)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_CH", "clickhousedb://source/example")
    config = {"provider": "clickhouse", "secret_ref": "KNOWLEDGE_SOURCE_TEST_CH", "source_id": "stable-source"}  # pragma: allowlist secret - operator reference
    first = curate_source("sql_catalog", config)
    unchanged = curate_source("sql_catalog", {**config, "previous_snapshot": first})
    assert unchanged["drift"]["changed_assets"] == []
    reflected["type"] = after
    changed = curate_source("sql_catalog", {**config, "previous_snapshot": first})
    assert changed["discovery_complete"] is True
    assert changed["assets"][0]["schema"][0]["data_type"] == first["assets"][0]["schema"][0]["data_type"]
    assert changed["assets"][0]["schema"][0]["native_type"] == after.compile(dialect=engine.dialect)
    assert changed["drift"]["changed_assets"][0]["change_types"] == ["revision_changed", "schema_changed"]


def test_offered_native_catalog_matches_qualified_policies():
    from knowledge_layer.store import CONNECTOR_TYPES

    sql = CONNECTOR_TYPES["sql_catalog"]
    choices = next(field["options"] for field in sql["config_fields"] if field["name"] == "provider")
    # Native cloud paths qualify fixed metadata APIs separately from generic
    # SQLAlchemy reflection and must not enable that fallback for these dialects.
    assert set(choices) == sql_catalog._BOUNDED_PROVIDERS | sql_catalog._NATIVE_METADATA_PROVIDERS
    assert CONNECTOR_TYPES["iceberg"]["capabilities"]["safe_profiling"] is False


def test_sql_key_metadata_distinguishes_unavailable_empty_and_composite(monkeypatch):
    state = {"primary_key": None}

    def primary_key(*_args, **_kwargs):
        if state["primary_key"] is None:
            raise NotImplementedError
        return {"constrained_columns": state["primary_key"]}

    def foreign_keys(*_args, **_kwargs):
        if state["primary_key"] is None:
            raise NotImplementedError
        return []

    inspector = SimpleNamespace(
        get_table_names=lambda **_kwargs: ["events"],
        get_columns=lambda *_args, **_kwargs: [{"name": name, "type": "INTEGER", "nullable": False}
                                               for name in ("player", "week")],
        get_pk_constraint=primary_key, get_foreign_keys=foreign_keys,
    )
    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(exec_driver_sql=lambda _sql: SimpleNamespace(scalar_one=lambda: "on"))),
        dispose=lambda: None, dialect=None,
    )
    monkeypatch.setattr(sql_catalog, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(sql_catalog, "inspect", lambda _connection: inspector)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_SQL", "postgresql://source/example")
    config = {"provider": "postgresql", "secret_ref": "KNOWLEDGE_SOURCE_TEST_SQL", "source_id": "stable-id"}  # pragma: allowlist secret - operator reference
    unknown = sql_catalog.discover(config)["assets"][0]
    assert unknown["profile"]["primary_key_state"] == "unknown"
    assert unknown["profile"]["foreign_key_state"] == "unknown"
    assert "primary_key" not in unknown["profile"]
    assert "foreign_keys" not in unknown["profile"]
    state["primary_key"] = []
    empty = sql_catalog.discover(config)["assets"][0]
    assert empty["profile"]["primary_key"] == []
    assert empty["profile"]["foreign_keys"] == []
    assert empty["profile"]["primary_key_state"] == "observed_metadata"
    assert empty["revision"] != unknown["revision"]
    state["primary_key"] = ["player", "week"]
    declared = sql_catalog.discover(config)["assets"][0]
    assert declared["profile"]["primary_key"] == ["player", "week"]
    assert declared["profile"]["primary_key_columns"] == ["player", "week"]
    assert declared["revision"] != empty["revision"]
