"""Native cloud metadata paths use finite read-only scopes and safe failures.

Snowflake session calls and BigQuery HTTP endpoints are offline doubles. An
optional installed Google-auth test exercises signed token exchange locally.
These fixtures do not qualify live cloud credentials, IAM or customer accounts.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import ModuleType

import httpx
import pytest

from knowledge_layer.access import Principal
from knowledge_layer.connectors import bigquery, cloud_metadata, snowflake, sql_catalog
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.curation import curate_source
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import validate_connection_config

SNOW_CONFIG = {"provider": "snowflake", "secret_ref": "KNOWLEDGE_SOURCE_TEST_SNOW", "schema": "SALES.PUBLIC"}
BQ_CONFIG = {"provider": "bigquery", "secret_ref": "KNOWLEDGE_SOURCE_TEST_BQ", "schema": "demo-project.sales"}


@pytest.fixture
def snow_catalog(monkeypatch):
    state = {"schemas": [("PUBLIC",)],
             "tables": [("ORDERS", "BASE TABLE", "Revenue means completed order value.", datetime(2026, 1, 1, tzinfo=UTC))],
             "columns": [("ORDERS", "ORDER_ID", 1, "TEXT", "NO", "Source order identifier.", None, None),
                         ("ORDERS", "AMOUNT", 2, "NUMBER", "YES", "Completed order value, in USD.", 38, 2)],
             "queries": [], "connections": [], "cursor_closes": 0, "connection_closes": 0}
    module = ModuleType("snowflake")
    module.__path__ = []
    sdk = ModuleType("snowflake.connector")

    class Cursor:
        def execute(self, statement, parameters, *, timeout):
            state["queries"].append((statement, parameters, timeout))
            assert statement.startswith("SELECT ") and '"SALES".INFORMATION_SCHEMA.' in statement
            self.rows = state["schemas"] if ".SCHEMATA " in statement else state["tables"] if ".TABLES " in statement else state["columns"]

        def fetchmany(self, maximum):
            return self.rows[:maximum]

        def close(self):
            state["cursor_closes"] += 1

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            state["connection_closes"] += 1

    def connect(**kwargs):
        state["connections"].append(kwargs)
        return Connection()

    sdk.connect = connect
    module.connector = sdk
    monkeypatch.setitem(sys.modules, "snowflake", module)
    monkeypatch.setitem(sys.modules, "snowflake.connector", sdk)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_SNOW", json.dumps({
        "account": "fixture-org-fixture-account", "user": "metadata-reader", "password": "private-fixture-password",
        "role": "METADATA_READER", "warehouse": "METADATA_WAREHOUSE",
    }))
    return state


def _table(table="orders", project="demo-project", *, fields=None):
    return {"tableReference": {"projectId": project, "datasetId": "sales", "tableId": table},
            "type": "TABLE", "description": "Completed customer order metadata.", "creationTime": "1767225600000",
            "schema": {"fields": fields or [
                {"name": "order_id", "type": "STRING", "mode": "REQUIRED", "description": "Source order identifier."},
                {"name": "amount", "type": "NUMERIC", "precision": "38", "scale": "2", "description": "USD order value."},
                {"name": "details", "type": "RECORD", "mode": "REPEATED", "fields": [
                    {"name": "category", "type": "STRING", "description": "Product category."},
                ]},
            ]}, "timePartitioning": {"type": "DAY", "field": "event_date"}}


@pytest.fixture
def bq_catalog(monkeypatch):
    state = {"calls": [], "table": _table(), "status": 200, "pages": [
        {"tables": [{"tableReference": _table()["tableReference"], "type": "TABLE"}]},
    ], "clients": []}
    original = httpx.Client

    def handler(request):
        state["calls"].append(request)
        assert request.method == "GET"
        assert request.url.host == "bigquery.googleapis.com"
        assert request.headers["Authorization"] == "Bearer private-fixture-bearer"
        if request.url.path.endswith("/tables"):
            index = int(request.url.params.get("pageToken", "0"))
            return httpx.Response(state["status"], json=state["pages"][index])
        assert request.url.path.endswith("/tables/orders")
        return httpx.Response(state["status"], json=state["table"])

    def client(**kwargs):
        state["clients"].append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(bigquery.httpx, "Client", client)
    monkeypatch.setattr(bigquery, "_access_token", lambda *_args: "private-fixture-bearer")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_BQ", json.dumps({"type": "service_account"}))
    return state


def _metadata_only(result):
    assert result["source"]["kind"] == "sql_catalog"
    assert result["capabilities"]["query_execution"] is False
    assert result["capabilities"]["permission_mirroring"] is False
    assert result["coverage"]["application_rows_read"] is False
    for row in result["assets"]:
        assert row["profile"]["metadata_only"] is True and row["profile"]["profile_rows"] == 0
        assert row["profile"]["row_count"] is None and row["profile"]["row_count_is_complete"] is False
        assert row["profile"]["primary_key_state"] == row["profile"]["foreign_key_state"] == "unknown"
    assert "private-fixture" not in json.dumps(result)


def test_snowflake_native_dispatch_uses_fixed_metadata_sql_and_finite_sdk_settings(snow_catalog):
    result = sql_catalog.discover({**SNOW_CONFIG, "tables": ["ORDERS"]})
    _metadata_only(result)
    assert len(snow_catalog["queries"]) == 3
    assert all(0 < timeout <= 10 for _, _, timeout in snow_catalog["queries"])
    assert all("WHERE TABLE_SCHEMA = %s" in statement and "TABLE_NAME IN (%s)" in statement
               for statement, _, _ in snow_catalog["queries"][1:])
    assert snow_catalog["queries"][0][1] == ("PUBLIC",)
    assert snow_catalog["queries"][1][1] == ("PUBLIC", "ORDERS")
    connection = snow_catalog["connections"][0]
    assert connection["database"] == "SALES" and connection["schema"] == "PUBLIC"
    assert connection["login_timeout"] == connection["socket_timeout"] == 5
    assert 0 < connection["network_timeout"] <= 10
    assert connection["client_session_keep_alive"] is connection["server_session_keep_alive"] is False
    assert connection["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"] == 10
    assert snow_catalog["cursor_closes"] == 3 and snow_catalog["connection_closes"] == 1
    row = result["assets"][0]
    assert row["schema"][1]["native_type"] == "NUMBER(38,2)"
    assert row["observations"]["native_schema"][1]["source_description"] == "Completed order value, in USD."
    assert row["source_pointer"] == "sql/snowflake/fixture-org-fixture-account/SALES/PUBLIC/ORDERS"


def test_bigquery_reads_bounded_metadata_rest_without_query_or_row_apis(bq_catalog):
    result = sql_catalog.discover(BQ_CONFIG)
    _metadata_only(result)
    assert len(bq_catalog["calls"]) == 2
    assert bq_catalog["clients"] == [{"follow_redirects": False, "trust_env": False}]
    for call in bq_catalog["calls"]:
        assert all(0 < value <= 10 for value in call.extensions["timeout"].values())
        assert not {"queries", "data", "jobs"}.intersection(call.url.path.split("/"))
    assert bq_catalog["calls"][1].url.params["view"] == "BASIC"
    row = result["assets"][0]
    assert row["source_pointer"] == "sql/bigquery/demo-project/sales/orders"
    assert row["schema"][2]["data_type"] == "structured"
    assert row["schema"][2]["native_mode"] == "REPEATED"
    assert row["schema"][2]["fields"][0]["name"] == "category"


@pytest.mark.parametrize("native_type,normalized", [("VARIANT", "structured"), ("OBJECT", "structured"),
                                                    ("ARRAY", "structured"), ("TIMESTAMP_NTZ", "timestamp"),
                                                    ("TIMESTAMP_TZ", "timestamp")])
def test_snowflake_container_and_timestamp_types_preserve_native_semantics(snow_catalog, native_type, normalized):
    snow_catalog["columns"] = [("ORDERS", "PAYLOAD", 1, native_type, "YES", None, None, None)]
    field = snowflake.discover(SNOW_CONFIG)["assets"][0]["schema"][0]
    assert field["data_type"] == normalized and field["native_type"] == native_type


def test_bigquery_selected_tables_avoid_unselected_inventory_reads(bq_catalog):
    result = bigquery.discover({**BQ_CONFIG, "tables": ["orders"]})
    assert len(result["assets"]) == 1 and len(bq_catalog["calls"]) == 1
    assert bq_catalog["calls"][0].url.path.endswith("/tables/orders")


def test_cloud_native_metadata_enters_curation_with_stable_ids_and_revision_bound_evidence(snow_catalog, bq_catalog):
    for provider, config, state in (("snowflake", SNOW_CONFIG, snow_catalog), ("bigquery", BQ_CONFIG, bq_catalog)):
        first = curate_source("sql_catalog", {**config, "source_id": "stable-customer-source"})
        same = curate_source("sql_catalog", {**config, "source_id": "stable-customer-source", "previous_snapshot": first})
        assert first["discovery_complete"] is True and first["agent_ready"] is False
        assert same["drift"]["changed_assets"] == []
        if provider == "snowflake":
            values = list(state["columns"][1])
            values[7] = 3
            state["columns"][1] = tuple(values)
        else:
            state["table"]["schema"]["fields"][1]["scale"] = "3"
        changed = curate_source("sql_catalog", {**config, "source_id": "stable-customer-source", "previous_snapshot": first})
        assert changed["assets"][0]["id"] == first["assets"][0]["id"]
        assert changed["assets"][0]["revision"] != first["assets"][0]["revision"]
        assert changed["drift"]["changed_assets"]
        assert {row["id"] for row in changed["evidence"]}.isdisjoint({row["id"] for row in first["evidence"]})
        assert first["evidence"][0]["details"]["observations"]["native_schema"]


def test_existing_source_policy_pins_cloud_provider_secret_scope_and_tables():
    selector = {"provider": "bigquery", "secret_ref": "KNOWLEDGE_SOURCE_TEST_BQ", "schema": "demo-project.sales", "tables": ["orders"]}
    policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "sql_catalog", "selector": selector,
        "allowed_subjects": ["owner"], "allowed_groups": [],
    }]}))
    owner = Principal("owner", frozenset())
    assert policy.allows("demo", "sql_catalog", validate_connection_config("sql_catalog", selector), owner)
    for changes in ({"provider": "snowflake"}, {"secret_ref": "KNOWLEDGE_SOURCE_OTHER"},
                    {"schema": "demo-project.payroll"}, {"tables": ["payroll"]}):
        assert not policy.allows("demo", "sql_catalog", {**selector, **changes}, owner)


@pytest.mark.parametrize("module,config", [(snowflake, SNOW_CONFIG), (bigquery, BQ_CONFIG)])
@pytest.mark.parametrize("changes", [{"schema": None}, {"schema": "unqualified"}, {"schema": "scope;DROP TABLE"},
                                   {"max_tables": True}, {"max_columns": 0}, {"tables": ["bad;name"]},
                                   {"tables": ["orders", "orders"]}])
def test_native_cloud_selector_validation_precedes_io(module, config, changes, monkeypatch):
    monkeypatch.setattr(module, "secret_object", lambda *_args: pytest.fail("credential access"))
    with pytest.raises(ConnectorError):
        module.discover({**config, **changes})


def test_snowflake_missing_selected_tables_and_record_budgets_fail_closed(snow_catalog):
    original_tables = snow_catalog["tables"]
    snow_catalog["tables"] = []
    with pytest.raises(ConnectorError) as error:
        snowflake.discover({**SNOW_CONFIG, "tables": ["MISSING"]})
    assert error.value.code == "table_not_found"
    snow_catalog["tables"] = original_tables
    for changes, code in (({"max_columns": 1}, "column_limit_exceeded"),):
        with pytest.raises(ConnectorError) as error:
            snowflake.discover({**SNOW_CONFIG, **changes})
        assert error.value.code == code
    snow_catalog["tables"] += [("OTHER", "VIEW", None, None)]
    with pytest.raises(ConnectorError) as error:
        snowflake.discover({**SNOW_CONFIG, "max_tables": 1})
    assert error.value.code == "table_limit_exceeded"


def test_snowflake_unavailable_schema_refuses_instead_of_reporting_empty_inventory(snow_catalog):
    snow_catalog["schemas"] = []
    snapshot = curate_source("sql_catalog", SNOW_CONFIG)
    assert snapshot["discovery_complete"] is False
    assert snapshot["errors"][0]["code"] == "cloud_scope_unavailable"
    assert len(snow_catalog["queries"]) == 1


def test_snowflake_secret_and_cleanup_errors_never_enter_failure_snapshot(snow_catalog, monkeypatch):
    def fail(**_kwargs):
        raise RuntimeError("private-fixture-password private-account-url")
    sys.modules["snowflake.connector"].connect = fail
    snapshot = curate_source("sql_catalog", SNOW_CONFIG)
    assert snapshot["discovery_complete"] is False
    assert snapshot["errors"][0]["code"] == "sql_metadata_unavailable"
    assert "private-fixture-password" not in json.dumps(snapshot)
    assert "private-account-url" not in json.dumps(snapshot)


def test_bigquery_table_and_nested_field_budgets_refuse_incomplete_inventory(bq_catalog):
    with pytest.raises(ConnectorError) as error:
        bigquery.discover({**BQ_CONFIG, "max_columns": 3})
    assert error.value.code == "column_limit_exceeded"
    bq_catalog["pages"][0]["tables"].append({"tableReference": _table("other")["tableReference"]})
    with pytest.raises(ConnectorError) as error:
        bigquery.discover({**BQ_CONFIG, "max_tables": 1})
    assert error.value.code == "table_limit_exceeded"


def test_bigquery_pagination_preserves_finite_complete_scope(bq_catalog):
    bq_catalog["pages"] = [{"tables": [{"tableReference": _table()["tableReference"]}], "nextPageToken": "1"}, {"tables": []}]
    result = bigquery.discover(BQ_CONFIG)
    assert result["coverage"]["complete_selected_scope"] is True
    assert len(bq_catalog["calls"]) == 3
    assert bq_catalog["calls"][1].url.params["pageToken"] == "1"
    bq_catalog["calls"].clear()
    bq_catalog["pages"][1] = {"tables": [], "nextPageToken": "1"}
    with pytest.raises(ConnectorError) as error:
        bigquery.discover(BQ_CONFIG)
    assert error.value.code == "cloud_metadata_invalid"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_bigquery_denied_or_failed_metadata_does_not_expose_credentials(bq_catalog, status):
    bq_catalog["status"] = status
    snapshot = curate_source("sql_catalog", BQ_CONFIG)
    assert snapshot["discovery_complete"] is False
    assert "private-fixture" not in json.dumps(snapshot)


def test_bigquery_refuses_scope_mismatch_and_redirect(bq_catalog):
    bq_catalog["table"]["tableReference"]["projectId"] = "hidden-project"
    with pytest.raises(ConnectorError) as error:
        bigquery.discover(BQ_CONFIG)
    assert error.value.code == "cloud_scope_mismatch" and "hidden-project" not in str(error.value)
    bq_catalog["status"] = 302
    with pytest.raises(ConnectorError) as error:
        bigquery.discover(BQ_CONFIG)
    assert error.value.code == "cloud_endpoint_denied"


@pytest.mark.parametrize("module,config", [(snowflake, SNOW_CONFIG), (bigquery, BQ_CONFIG)])
def test_cloud_byte_and_time_budgets_refuse_instead_of_truncating(module, config, snow_catalog, bq_catalog, monkeypatch):
    monkeypatch.setattr(cloud_metadata, "MAX_RESPONSE_BYTES", 32)
    with pytest.raises(ConnectorError) as error:
        module.discover(config)
    assert error.value.code == "cloud_metadata_too_large"
    monkeypatch.setattr(cloud_metadata, "MAX_DISCOVERY_SECONDS", 0)
    with pytest.raises(ConnectorError) as error:
        module.discover(config)
    assert error.value.code == "cloud_discovery_timeout"


def test_snowflake_keypair_profile_uses_der_and_never_allows_endpoint_overrides():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    profile = {"account": "fixture-account", "user": "metadata-reader", "private_key_pem": pem}
    credentials = snowflake._credentials(profile)
    assert credentials["authenticator"] == "SNOWFLAKE_JWT"
    assert isinstance(credentials["private_key"], bytes) and "private_key_pem" not in credentials
    for changes in ({"host": "evil.example"}, {"insecure_mode": True}, {"password": "also-present"}, {"account": "https://evil.example"}):
        with pytest.raises(ConnectorError):
            snowflake._credentials({**profile, **changes})


def test_installed_google_auth_signs_only_a_bounded_offline_token_exchange():
    pytest.importorskip("google.oauth2.service_account", reason="Install the bigquery extra for installed-auth qualification")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    profile = {"type": "service_account", "project_id": "fixture-project", "private_key": pem,
               "client_email": "reader@fixture-project.iam.gserviceaccount.com", "token_uri": bigquery._TOKEN}
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST" and str(request.url) == bigquery._TOKEN
        assert "assertion=" in request.content.decode() and pem not in request.content.decode()
        return httpx.Response(200, json={"access_token": "private-signed-fixture-token", "expires_in": 3600, "token_type": "Bearer"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = bigquery._access_token(profile, cloud_metadata.MetadataBudget(), client)
        assert token == "private-signed-fixture-token" and len(calls) == 1
        assert all(0 < timeout <= 10 for timeout in calls[0].extensions["timeout"].values())
        with pytest.raises(ConnectorError, match="invalid or unsupported"):
            bigquery._access_token({**profile, "token_uri": "https://evil.example/token"}, cloud_metadata.MetadataBudget(), client)
    assert len(calls) == 1
