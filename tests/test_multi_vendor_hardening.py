"""Verification of multi-vendor big data fixes, improvements, and contract guardrails."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from knowledge_layer.connectors.bigquery import MetadataBudget, _access_token
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.local_files import (
    SUPPORTED_TABLE_TYPES,
    _bounded_join_probes,
    _profile_parquet,
)
from knowledge_layer.connectors.local_files import (
    discover as local_discover,
)
from knowledge_layer.connectors.postgres import discover as postgres_discover
from knowledge_layer.connectors.sql_catalog import (
    _apply_clickhouse_driver_patches,
    _read_only_session,
)
from knowledge_layer.contracts import (
    CONTRACT_KINDS,
    semantic_contract,
    strip_materialized_assertions,
)
from knowledge_layer.store import KnowledgeStore


def test_parquet_supported_and_profiles_columns_and_dates(tmp_path: Path):
    assert ".parquet" in SUPPORTED_TABLE_TYPES
    now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
    today = date(2026, 10, 8)
    table = pa.Table.from_arrays(
        [
            pa.array(["TRIP-1", "TRIP-2", "TRIP-3"]),
            pa.array([12.50, 45.00, 8.25]),
            pa.array([now, now, now]),
            pa.array([today, today, today]),
        ],
        names=["trip_id", "fare_amount", "pickup_datetime", "pickup_date"],
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    parquet_bytes = buffer.getvalue()

    schema, profile = _profile_parquet(parquet_bytes, profile_rows=10, max_columns=10)
    assert len(schema) == 4
    names = [f["name"] for f in schema]
    assert names == ["trip_id", "fare_amount", "pickup_datetime", "pickup_date"]
    assert profile["row_count"] == 3
    assert profile["profile_rows"] == 3
    assert profile["row_count_is_complete"] is True
    assert profile["profile_is_sampled"] is False

    # Verify discovery through local_files connector
    parquet_file = tmp_path / "trips.parquet"
    parquet_file.write_bytes(parquet_bytes)

    result = local_discover({
        "root": str(tmp_path),
        "path": ".",
        "include": "*.parquet",
        "max_files": 5,
        "max_file_bytes": 10 * 1024 * 1024,
        "max_total_bytes": 50 * 1024 * 1024,
        "profile_rows": 100,
        "max_columns": 20,
    })
    assert len(result["assets"]) == 1
    asset = result["assets"][0]
    assert asset["name"] == "trips.parquet"
    assert asset["asset_type"] == "table"
    assert asset["profile"]["row_count"] == 3


def test_parquet_bounded_join_probes(tmp_path: Path):
    table1 = pa.Table.from_arrays(
        [pa.array(["TRIP-1", "TRIP-2", "TRIP-3"]), pa.array(["DRV-1", "DRV-2", "DRV-1"])],
        names=["trip_id", "driver_id"],
    )
    buf1 = io.BytesIO()
    pq.write_table(table1, buf1)

    table2 = pa.Table.from_arrays(
        [pa.array(["DRV-1", "DRV-2", "DRV-3"]), pa.array(["John", "Jane", "Alice"])],
        names=["driver_id", "driver_name"],
    )
    buf2 = io.BytesIO()
    pq.write_table(table2, buf2)

    assets = [
        {
            "id": "asset-trips",
            "asset_type": "table",
            "source_pointer": "trips.parquet",
            "schema": [{"name": "trip_id"}, {"name": "driver_id"}],
            "profile": {"row_count": 3, "profile_rows": 3, "row_count_is_complete": True,
                        "null_counts": {"driver_id": 0}},
        },
        {
            "id": "asset-drivers",
            "asset_type": "table",
            "source_pointer": "drivers.parquet",
            "schema": [{"name": "driver_id"}, {"name": "driver_name"}],
            "profile": {"row_count": 3, "profile_rows": 3, "row_count_is_complete": True,
                        "null_counts": {"driver_id": 0}},
        },
    ]
    contents = {"asset-trips": buf1.getvalue(), "asset-drivers": buf2.getvalue()}

    probes = _bounded_join_probes(assets, contents, profile_rows=100)
    assert len(probes) >= 1
    probe = next(p for p in probes if p["source_asset_id"] == "asset-trips" and p["target_asset_id"] == "asset-drivers")
    assert probe["source_field"] == "driver_id"
    assert probe["target_field"] == "driver_id"


def test_postgres_connector_executes_session_statements_without_startup_options():
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_connection = MagicMock()
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor
    mock_connection.__enter__.return_value = mock_connection

    with patch("psycopg.connect", return_value=mock_connection) as mock_connect, \
         patch("knowledge_layer.connectors.postgres.read_source_secret", return_value="postgresql://source/example"):
        postgres_discover({"secret_ref": "KNOWLEDGE_SOURCE_TEST_PG", "schema": "public", "connect_timeout": 5})  # pragma: allowlist secret - operator reference

        mock_connect.assert_called_once()
        _, kwargs = mock_connect.call_args
        # startup options must NOT include statement_timeout (for pooler compatibility)
        assert "statement_timeout" not in kwargs.get("options", "")
        assert "default_transaction_read_only=on" in kwargs.get("options", "")

        # Session statements must be executed explicitly on the cursor
        calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert "SET statement_timeout = 5000" in calls
        assert "SET default_transaction_read_only = on" in calls
        assert "SET TRANSACTION READ ONLY" in calls


def test_sql_catalog_postgres_read_only_session():
    mock_connection = MagicMock()
    mock_connection.exec_driver_sql.return_value.scalar_one.return_value = "on"

    _read_only_session(mock_connection, "postgresql")
    calls = [c[0][0] for c in mock_connection.exec_driver_sql.call_args_list]
    assert any("SET statement_timeout" in c for c in calls)
    assert any("SET default_transaction_read_only" in c for c in calls)
    assert "SET TRANSACTION READ ONLY" in calls


def test_sql_catalog_clickhouse_driver_patches():
    _apply_clickhouse_driver_patches()


def test_bigquery_adc_token_handling():
    import sys
    from types import ModuleType

    budget = MetadataBudget()
    mock_client = MagicMock()

    # Test when google.auth is not present
    with patch.dict(sys.modules, {"google": None, "google.auth": None}):
        with pytest.raises(ConnectorError) as exc_info:
            _access_token({"type": "application_default_credentials"}, budget, mock_client)
        assert exc_info.value.code == "sql_driver_unavailable"

    # Test when google.auth is present and returns token
    mock_google = ModuleType("google")
    mock_auth = ModuleType("google.auth")
    mock_google.auth = mock_auth
    mock_requests = ModuleType("google.auth.transport.requests")
    mock_creds = MagicMock()
    mock_creds.token = "ya29.mock_adc_token_value_abcdef"
    mock_auth.default = MagicMock(return_value=(mock_creds, "mock-project"))
    mock_requests.Request = MagicMock()

    with patch.dict(sys.modules, {
        "google": mock_google,
        "google.auth": mock_auth,
        "google.auth.transport": ModuleType("google.auth.transport"),
        "google.auth.transport.requests": mock_requests,
    }):
        token = _access_token({"type": "application_default_credentials"}, budget, mock_client)
        assert token == "ya29.mock_adc_token_value_abcdef"


def test_grain_contract_with_required_partition_filter_and_keys(tmp_path: Path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Analytics", config={})
    source_id = source["id"]

    base = {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{
            "id": "asset-trips", "name": "trips", "asset_type": "table",
            "schema": [
                {"name": "trip_id", "data_type": "text"},
                {"name": "pickup_date", "data_type": "date"},
            ],
            "profile": {
                "row_count": 3_465_634_142,
                "row_count_estimate": 3_465_634_142,
                "row_count_estimate_state": "estimated",
                "profile_rows": 1000,
            },
        }],
        "concepts": [], "relationships": [], "metrics": [], "proposals": [],
        "evidence": [{"id": "ev-trips", "asset_id": "asset-trips",
                      "evidence_type": "asset_metadata", "summary": "Trips schema"}],
        "errors": [],
    }
    snapshot = store.save_snapshot("demo", source_id, {**base, "snapshot_id": "snap-1"})

    # Submit grain with partition constraints
    proposal = store.submit_contract_assertion(
        "demo", source_id, assertion_kind="grain", asset_id="asset-trips",
        payload={
            "description": "One row per taxi trip",
            "key_fields": ["trip_id"],
            "partition_keys": ["pickup_date"],
            "required_partition_filter": "pickup_date >= '2023-01-01'",
        },
        evidence_ids=["ev-trips"],
        rationale="Multi-billion row table requires partition pruning on pickup_date.",
        expected_snapshot_id=snapshot["id"],
    )
    assert proposal["status"] == "pending_review"

    # Approve proposal
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve")

    contract = semantic_contract(store, tenant_id="demo", connection_id=source_id)
    dataset = contract["datasets"][0]
    assert dataset["grain"]["key_fields"] == ["trip_id"]
    assert dataset["grain"]["partition_keys"] == ["pickup_date"]
    assert dataset["grain"]["required_partition_filter"] == "pickup_date >= '2023-01-01'"
    assert dataset["row_count"] == 3_465_634_142
    assert dataset["row_count_estimate"] == 3_465_634_142
    assert dataset["row_count_estimate_state"] == "estimated"

    store.close()


def test_tenant_boundary_contract_assertion_and_isolation(tmp_path: Path):
    assert "tenant_boundary" in CONTRACT_KINDS

    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Platform Staging", config={})
    source_id = source["id"]

    base = {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{
            "id": "asset-athletes", "name": "athletes", "asset_type": "table",
            "schema": [{"name": "id", "data_type": "text"}, {"name": "account_id", "data_type": "text"}],
            "profile": {"row_count": 105},
        }],
        "concepts": [], "relationships": [], "metrics": [], "proposals": [],
        "evidence": [{"id": "ev-athletes", "asset_id": "asset-athletes",
                      "evidence_type": "asset_metadata", "summary": "Athletes schema"}],
        "errors": [],
    }
    snapshot = store.save_snapshot("demo", source_id, {**base, "snapshot_id": "snap-athletes"})

    # Propose tenant boundary
    proposal = store.submit_contract_assertion(
        "demo", source_id, assertion_kind="tenant_boundary", asset_id="asset-athletes",
        payload={
            "description": "Athlete identities belong exclusively to this staging training environment.",
            "isolated_entity_types": "athletes, training_sessions, workouts",
            "prohibited_cross_joins": "Cross-system joins to external eCommerce users or retail accounts are prohibited.",
        },
        evidence_ids=["ev-athletes"],
        rationale="Steward-mandated tenant isolation policy.",
        expected_snapshot_id=snapshot["id"],
    )
    assert proposal["status"] == "pending_review"

    # Approve proposal
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve")

    contract = semantic_contract(store, tenant_id="demo", connection_id=source_id)
    assert len(contract["tenant_boundaries"]) == 1
    boundary = contract["tenant_boundaries"][0]
    assert boundary["status"] == "approved"
    assert boundary["isolated_entity_types"] == "athletes, training_sessions, workouts"
    assert "Cross-system joins" in boundary["prohibited_cross_joins"]

    dataset = contract["datasets"][0]
    assert dataset["tenant_boundary"] == boundary

    # Verify strip_materialized_assertions resets tenant_boundary cleanly
    snap_dict = store.get_snapshot("demo", source_id)
    assert "tenant_boundary" in snap_dict["assets"][0]
    strip_materialized_assertions(snap_dict)
    assert "tenant_boundary" not in snap_dict["assets"][0]

    store.close()
