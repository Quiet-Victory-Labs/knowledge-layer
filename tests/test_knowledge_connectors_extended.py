from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from adapter_response_fixture import streamed_response
from sqlalchemy import BigInteger, DateTime, Integer, Numeric, create_engine, text
from sqlalchemy.dialects import postgresql

from knowledge_layer.connectors.common import ConnectorError, type_name
from knowledge_layer.connectors.sql_catalog import _url as sql_url
from knowledge_layer.connectors.sql_catalog import discover as discover_sql
from knowledge_layer.curation import curate_source
from knowledge_layer.store import KnowledgeValidationError, validate_connection_config


def test_sql_adapter_reflects_metadata_without_rows_or_secret(tmp_path, monkeypatch):
    database = tmp_path / "source.db"
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, customer_name TEXT)"))
        connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, total_amount NUMERIC, customer_id INTEGER REFERENCES customers(id))"))
        connection.execute(text("INSERT INTO orders VALUES (1, 412.25, 7)"))
    engine.dispose()
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_SQL", url)

    config = validate_connection_config("sql_catalog", {
        "provider": "sqlite", "secret_ref": "KNOWLEDGE_SOURCE_TEST_SQL", "tables": ["orders", "customers"],
    })
    snapshot = curate_source("sql_catalog", {**config, "name": "Orders warehouse"})
    assert snapshot["status"] == "discovery_complete"
    assert [item["name"] for item in snapshot["assets"]] == ["customers", "orders"]
    orders = next(item for item in snapshot["assets"] if item["name"] == "orders")
    assert {item["name"] for item in orders["schema"]} == {"id", "total_amount", "customer_id"}
    assert orders["profile"]["metadata_only"] is True
    assert orders["profile"]["row_count"] is None
    assert any(item["predicate"] == "declared_foreign_key" and item["status"] == "observed_metadata" for item in snapshot["relationships"])
    assert snapshot["capabilities"]["query_execution"] is False
    assert str(database) not in json.dumps(snapshot)
    assert "412.25" not in json.dumps(snapshot)

    again = curate_source("sql_catalog", {**config, "name": "Orders warehouse", "previous_snapshot": snapshot})
    assert again["drift"]["changed_assets"] == []
    assert again["drift"]["removed_assets"] == []


def test_sql_adapter_rejects_unscoped_credentials_and_provider_mismatch(tmp_path, monkeypatch):
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("sql_catalog", {"provider": "mysql", "secret_ref": "DATABASE_URL"})
    with pytest.raises(KnowledgeValidationError):
        validate_connection_config("sql_catalog", {
            "provider": "mysql", "secret_ref": "KNOWLEDGE_SOURCE_MYSQL", "url": "mysql://bad",
        })
    monkeypatch.setenv("KNOWLEDGE_SOURCE_MYSQL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    with pytest.raises(ConnectorError) as error:
        discover_sql({"provider": "mysql", "secret_ref": "KNOWLEDGE_SOURCE_MYSQL"})
    assert error.value.code == "database_provider_mismatch"


def test_clickhouse_provider_accepts_official_clickhousedb_dialect(monkeypatch):
    monkeypatch.setenv(
        "KNOWLEDGE_SOURCE_TEST_CLICKHOUSE",
        "clickhousedb://demo:@sql-clickhouse.clickhouse.com:443/imdb?secure=true",
    )
    url = sql_url("KNOWLEDGE_SOURCE_TEST_CLICKHOUSE", "clickhouse")
    assert url.get_backend_name() == "clickhousedb"
    assert url.database == "imdb"


@pytest.mark.parametrize(
    ("before_type", "after_type"),
    [
        (Integer(), BigInteger()),
        (Numeric(10, 2), Numeric(10, 3)),
        (Numeric(10, 2), Numeric(12, 2)),
        (DateTime(timezone=False), DateTime(timezone=True)),
        (postgresql.ARRAY(Integer()), postgresql.ARRAY(BigInteger())),
    ],
)
def test_sql_adapter_physical_type_drift_survives_normalized_schema(
    monkeypatch, before_type, after_type,
):
    from knowledge_layer.connectors import sql_catalog

    reflected = {"type": before_type}
    inspector = SimpleNamespace(
        get_table_names=lambda **_kwargs: ["events"],
        get_columns=lambda *_args, **_kwargs: [
            {"name": "value", "type": reflected["type"], "nullable": True}
        ],
        get_pk_constraint=lambda *_args, **_kwargs: {"constrained_columns": []},
        get_foreign_keys=lambda *_args, **_kwargs: [],
    )
    engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(exec_driver_sql=lambda _statement: SimpleNamespace(scalar_one=lambda: "on"))), dispose=lambda: None,
        dialect=postgresql.dialect(),
    )
    monkeypatch.setattr(sql_catalog, "create_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(sql_catalog, "inspect", lambda _connection: inspector)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_SQL", "postgresql://source/example")
    config = {"provider": "postgresql", "secret_ref": "KNOWLEDGE_SOURCE_TEST_SQL", "source_id": "stable-id"}
    first = curate_source("sql_catalog", config)
    unchanged = curate_source("sql_catalog", {**config, "previous_snapshot": first})
    assert unchanged["drift"]["changed_assets"] == []
    reflected["type"] = after_type
    changed = curate_source("sql_catalog", {**config, "previous_snapshot": first})
    assert changed["discovery_complete"] is True
    def normalize(schema):
        return [{key: field[key] for key in ("name", "data_type", "nullable")} for field in schema]
    assert normalize(changed["assets"][0]["schema"]) == normalize(first["assets"][0]["schema"])
    assert changed["assets"][0]["schema"][0]["native_type"] == after_type.compile(dialect=postgresql.dialect())
    assert changed["assets"][0]["revision"] != first["assets"][0]["revision"]
    assert changed["drift"]["changed_assets"][0]["change_types"] == ["revision_changed", "schema_changed"]
    assert changed["assets"][0]["profile"]["metadata_only"] is True


@pytest.mark.parametrize("physical_type", [
    "struct<1: count: optional int>", "list<int>", "map<string, long>",
    "integer[]", "array<double>", "jsonb",
])
def test_structured_types_keep_their_container_identity(physical_type):
    assert type_name(physical_type) == "structured"


@pytest.mark.parametrize("target_suffix", [".jsonl", ".csv"])
def test_jsonl_join_probes_keep_zero_keys_distinct_from_null_and_missing(
    tmp_path, target_suffix,
):
    from knowledge_layer.connectors.local_files import discover

    (tmp_path / "work_orders.jsonl").write_text(
        '\n{"client_id":0}\n{"client_id":null}\n{}\n'
        '{"client_id":"private-source-client-7162"}\n', encoding="utf-8",
    )
    target_data = (
        '{"client_id":0}\n{"client_id":"private-target-client-3918"}\n'
        if target_suffix == ".jsonl" else
        'client_id\n0\nprivate-target-client-3918\n'
    )
    (tmp_path / f"clients{target_suffix}").write_text(target_data, encoding="utf-8")
    config = {"root": str(tmp_path), "source_id": "jsonl-zero-test"}
    discovery = discover(config)
    assets = {asset["name"]: asset["id"] for asset in discovery["assets"]}
    probe = next(item for item in discovery["join_probes"]
                 if item["source_asset_id"] == assets["work_orders.jsonl"])
    assert probe["source_nonnull_rows_probed"] == 2
    # JSON integer zero equals the JSON target integer, while a CSV cell is
    # source text. A shared spelling alone does not declare a cross-type join.
    assert probe["matched_source_rows"] == (1 if target_suffix == ".jsonl" else 0)
    assert probe["target_nonnull_rows_probed"] == 2
    assert probe["target_distinct_values_probed"] == 2
    assert probe["complete_source_and_target"] is True
    assert probe["raw_values_retained"] is False
    snapshot = curate_source("local_files", config)
    assert snapshot["discovery_complete"] is True
    assert any(item["evidence_type"] == "bounded_join_probe" for item in snapshot["evidence"])
    serialized = json.dumps(snapshot)
    assert "private-source-client-7162" not in serialized
    assert "private-target-client-3918" not in serialized
    assert all(asset["profile"]["content_retained"] is False for asset in snapshot["assets"])


def test_jsonl_join_probes_bound_rows_and_report_incomplete_samples(tmp_path):
    from knowledge_layer.connectors.local_files import discover

    (tmp_path / "work_orders.jsonl").write_text(
        '{"client_id":0}\n{"client_id":"private-late-match-5190"}\n'
        '{"client_id":null}\n{}\n', encoding="utf-8",
    )
    (tmp_path / "clients.jsonl").write_text(
        '{"client_id":0}\n{"client_id":"private-early-target-6018"}\n'
        '{"client_id":"private-late-match-5190"}\n', encoding="utf-8",
    )
    discovery = discover({"root": str(tmp_path), "profile_rows": 2})
    source_id = next(asset["id"] for asset in discovery["assets"] if asset["name"] == "work_orders.jsonl")
    probe = next(item for item in discovery["join_probes"] if item["source_asset_id"] == source_id)
    assert probe["source_nonnull_rows_probed"] == 2
    assert probe["target_nonnull_rows_probed"] == 2
    assert probe["matched_source_rows"] == 1
    assert probe["complete_source_and_target"] is False
    assert probe["raw_values_retained"] is False
    assert "private-late-match-5190" not in json.dumps(discovery)


@pytest.mark.parametrize("invalid_line", ['{"client_id":"private-broken-value-9251"', '[]', 'null'])
def test_jsonl_join_probes_refuse_invalid_records_beyond_sample_limit(tmp_path, invalid_line):
    from knowledge_layer.connectors.local_files import discover

    (tmp_path / "work_orders.jsonl").write_text(
        '{"client_id":0}\n' + invalid_line + '\n', encoding="utf-8",
    )
    (tmp_path / "clients.jsonl").write_text('{"client_id":0}\n', encoding="utf-8")
    discovery = discover({"root": str(tmp_path), "profile_rows": 1})
    assert discovery["errors"][0]["code"] == "parse_error"
    assert discovery["join_probes"] == []
    snapshot = curate_source("local_files", {"root": str(tmp_path), "profile_rows": 1})
    assert snapshot["discovery_complete"] is False
    assert "private-broken-value-9251" not in json.dumps(snapshot)


def test_jsonl_join_probe_does_not_claim_complete_comparison_of_structured_keys(tmp_path):
    from knowledge_layer.connectors.local_files import discover

    (tmp_path / "work_orders.jsonl").write_text(
        '{"client_id":0}\n{"client_id":{"private_key":"never-retain-8193"}}\n', encoding="utf-8",
    )
    (tmp_path / "clients.jsonl").write_text('{"client_id":0}\n', encoding="utf-8")
    discovery = discover({"root": str(tmp_path)})
    source_id = next(asset["id"] for asset in discovery["assets"] if asset["name"] == "work_orders.jsonl")
    probe = next(item for item in discovery["join_probes"] if item["source_asset_id"] == source_id)
    assert probe["source_nonnull_rows_probed"] == 1
    assert probe["matched_source_rows"] == 1
    assert probe["complete_source_and_target"] is False
    assert "never-retain-8193" not in json.dumps(discovery)


def test_object_store_adapter_lists_only_bounded_metadata(monkeypatch):
    object_store = pytest.importorskip("knowledge_layer.connectors.object_store")
    profile = {
        "bucket": "company-data", "endpoint_url": "https://objects.example.com",
        "access_key_id": "private-access-key", "secret_access_key": "private-secret-key",
    }
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_OBJECTS", json.dumps(profile))

    class Client:
        def list_objects_v2(self, **kwargs):
            assert kwargs == {"Bucket": "company-data", "Prefix": "finance/", "MaxKeys": 3}
            return {"Contents": [
                {"Key": "finance/orders.csv", "Size": 42, "ETag": '"a"'},
                {"Key": "finance/policy.pdf", "Size": 81, "ETag": '"b"'},
            ], "IsTruncated": False}

        def close(self):
            pass

    monkeypatch.setattr(object_store.boto3, "client", lambda *_args, **_kwargs: Client())
    config = validate_connection_config("object_store", {
        "secret_ref": "KNOWLEDGE_SOURCE_TEST_OBJECTS", "prefix": "finance/", "max_objects": 2,
    })
    snapshot = curate_source("object_store", {**config, "name": "Finance files"})
    assert snapshot["status"] == "discovery_complete"
    assert [(item["name"], item["asset_type"]) for item in snapshot["assets"]] == [
        ("orders.csv", "table"), ("policy.pdf", "document"),
    ]
    assert all(item["profile"]["metadata_only"] for item in snapshot["assets"])
    assert "private-secret-key" not in json.dumps(snapshot)
    assert snapshot["capabilities"]["query_execution"] is False


def test_object_store_rejects_partial_listing_and_plain_http(monkeypatch):
    object_store = pytest.importorskip("knowledge_layer.connectors.object_store")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_OBJECTS", json.dumps({
        "bucket": "company-data", "endpoint_url": "http://objects.example.com",
    }))
    with pytest.raises(ConnectorError) as error:
        object_store.discover({"secret_ref": "KNOWLEDGE_SOURCE_TEST_OBJECTS"})
    assert error.value.code == "object_endpoint_tls_required"

    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_OBJECTS", json.dumps({"bucket": "company-data"}))

    class Client:
        def list_objects_v2(self, **_kwargs):
            return {"Contents": [{"Key": "a.csv", "Size": 10}], "IsTruncated": True}

        def close(self):
            pass

    monkeypatch.setattr(object_store.boto3, "client", lambda *_args, **_kwargs: Client())
    with pytest.raises(ConnectorError) as error:
        object_store.discover({"secret_ref": "KNOWLEDGE_SOURCE_TEST_OBJECTS", "max_objects": 1})
    assert error.value.code == "object_limit_exceeded"


def test_iceberg_credentials_require_explicit_absolute_file(monkeypatch, tmp_path):
    from knowledge_layer.connectors.iceberg import _client_credentials

    monkeypatch.delenv("POLARIS_CLIENT_FILE", raising=False)
    with pytest.raises(ConnectorError, match="POLARIS_CLIENT_FILE"):
        _client_credentials()
    monkeypatch.setenv("POLARIS_CLIENT_FILE", "relative/client.json")
    with pytest.raises(ConnectorError, match="absolute path"):
        _client_credentials()
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"client_id": "test-client", "client_secret": "test-secret"}), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("POLARIS_CLIENT_FILE", str(path))
    assert _client_credentials() == ("test-client", "test-secret")
    path.chmod(0o644)
    with pytest.raises(ConnectorError, match="credentials are not configured"):
        _client_credentials()


def test_external_adapter_protocol_normalizes_metadata_and_hides_token(monkeypatch):
    from knowledge_layer.connectors import external_adapter

    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_ADAPTER", json.dumps({
        "endpoint": "https://adapter.example.com/discover",
        "provider": "crm",
        "allowed_scopes": ["accounts"],
        "bearer_token": "private-adapter-token",
    }))
    real_client = httpx.Client
    field_name = ["annual_revenue"]

    def handler(request):
        assert request.url.path == "/discover"
        assert request.headers["Authorization"] == "Bearer private-adapter-token"
        assert json.loads(request.content) == {
            "protocol_version": "1.0", "scope": "accounts", "max_assets": 2, "max_columns": 10,
            "include_document_excerpts": False,
        }
        return streamed_response(200, json={
            "protocol_version": "1.0",
            "assets": [{
                "name": "Accounts", "pointer": "objects/accounts", "revision": "rev-7",
                "asset_type": "table",
                "schema": [
                    {"name": "account_id", "data_type": "varchar", "nullable": False},
                    {"name": field_name[0], "data_type": "decimal", "nullable": True},
                ],
            }],
        })

    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    config = validate_connection_config("external_adapter", {
        "secret_ref": "KNOWLEDGE_SOURCE_TEST_ADAPTER", "scope": "accounts",
        "max_assets": 2, "max_columns": 10,
    })
    snapshot = curate_source("external_adapter", {**config, "name": "CRM"})
    assert snapshot["status"] == "discovery_complete"
    assert [asset["name"] for asset in snapshot["assets"]] == ["Accounts"]
    assert snapshot["assets"][0]["source_pointer"] == "adapter/crm/accounts/objects/accounts"
    assert any(item["name"] == "Annual Revenue" for item in snapshot["concepts"])
    assert "private-adapter-token" not in json.dumps(snapshot)
    field_name[0] = "lifetime_value"
    changed = curate_source("external_adapter", {**config, "name": "CRM"})
    assert changed["assets"][0]["revision"] != snapshot["assets"][0]["revision"]
    assert changed["evidence"][0]["id"] != snapshot["evidence"][0]["id"]


def test_external_adapter_document_terms_require_excerpt_opt_in(monkeypatch):
    from knowledge_layer.connectors import external_adapter

    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_ADAPTER", json.dumps({
        "endpoint": "https://adapter.example.com/discover", "provider": "wiki",
        "allowed_scopes": ["policies"],
    }))
    real_client = httpx.Client

    def handler(request):
        body = json.loads(request.content)
        assert body["include_document_excerpts"] is True
        return streamed_response(200, json={"protocol_version": "1.0", "assets": [{
            "name": "Field policy", "pointer": "pages/field-policy", "asset_type": "document",
            "revision": "rev-1", "document_terms": [{
                "term": "Billable hours", "excerpt": "Technician labor hours belong to one work order.",
                "line": 8,
            }],
        }]})

    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    config = validate_connection_config("external_adapter", {
        "secret_ref": "KNOWLEDGE_SOURCE_TEST_ADAPTER", "scope": "policies",
        "retain_semantic_samples": True,
    })
    snapshot = curate_source("external_adapter", config)
    assert any(item.get("evidence_type") == "document_derived_term" for item in snapshot["evidence"])
    assert snapshot["assets"][0]["profile"]["content_retained"] is True


def test_external_adapter_denies_scope_and_unbounded_payload(monkeypatch):
    from knowledge_layer.connectors import external_adapter

    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_ADAPTER", json.dumps({
        "endpoint": "https://adapter.example.com/discover",
        "provider": "crm", "allowed_scopes": ["accounts"],
    }))
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover({"secret_ref": "KNOWLEDGE_SOURCE_TEST_ADAPTER", "scope": "payroll"})
    assert error.value.code == "adapter_scope_denied"

    real_client = httpx.Client
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, json={
            "protocol_version": "1.0", "assets": [{"name": "A", "pointer": "a"}, {"name": "B", "pointer": "b"}],
        })), **kwargs,
    ))
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover({"secret_ref": "KNOWLEDGE_SOURCE_TEST_ADAPTER", "scope": "accounts", "max_assets": 1})
    assert error.value.code == "asset_limit_exceeded"
