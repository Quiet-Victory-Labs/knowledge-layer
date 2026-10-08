"""Comprehensive tests for KnowledgeClient native SDK, enterprise catalog presets, and lakehouse builders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_layer import (
    KnowledgeClient,
    get_preset_mapping,
    lakekeeper_profile,
    nessie_profile,
    polaris_profile,
    translate_catalog_export,
    unity_catalog_profile,
)
from knowledge_layer.connectors.common import ConnectorError

# ---------------------------------------------------------------------------
# 1. Native Python SDK (KnowledgeClient) Tests
# ---------------------------------------------------------------------------

def test_knowledge_client_lifecycle_and_curation(tmp_path: Path):
    db_file = tmp_path / "sdk_test.db"
    fixtures_dir = tmp_path / "data"
    fixtures_dir.mkdir(parents=True)

    # Create synthetic test dataset
    csv_file = fixtures_dir / "customers.csv"
    csv_file.write_text("customer_id,name,tier,created_date\n101,Acme Corp,Enterprise,2026-01-01\n102,Globex,Standard,2026-02-01\n")

    policy_file = fixtures_dir / "service_terms.md"
    policy_file.write_text("# Customer Service Terms\n\nEnterprise tier accounts are entitled to 4-hour response SLAs.\nStandard tier accounts receive next-business-day response.\n")

    # Initialize embedded client
    client = KnowledgeClient(
        database_url=f"sqlite:///{db_file}",
        tenant_id="test_tenant",
        local_files_root=fixtures_dir,
    )

    # Connect and curate source
    conn_result = client.connect_source(
        "local_files",
        {"path": ".", "include": "**/*"},
        name="Customer Files",
    )
    assert conn_result["status"] == "discovery_complete"
    assert conn_result["assets_count"] >= 1
    assert conn_result["evidence_count"] >= 1
    connection_id = conn_result["connection_id"]

    # Verify connection retrieval
    connection = client.get_connection(connection_id)
    assert connection is not None
    assert connection["name"] == "Customer Files"

    connections = client.list_connections()
    assert len(connections) == 1
    assert connections[0]["id"] == connection_id

    # Retrieve semantic contract
    contract = client.get_semantic_contract(connection_id)
    assert "datasets" in contract
    assert len(contract["datasets"]) >= 1

    # Search document and metadata evidence
    evidence = client.search_evidence("customers", connection_id=connection_id)
    assert len(evidence) >= 1
    assert any("customers" in str(e.get("source_pointer", "")).lower() or "customer" in str(e.get("summary", "")).lower() for e in evidence)

    # Propose contract assertion (grain with partition pruning)
    table_asset = next(a for a in conn_result["snapshot"]["assets"] if a["asset_type"] == "table")
    evidence_id = conn_result["snapshot"]["evidence"][0]["id"]

    proposal = client.propose_assertion(
        connection_id=connection_id,
        assertion_kind="grain",
        asset_id=table_asset["id"],
        payload={
            "description": "Unique customer record identified by customer_id",
            "key_fields": ["customer_id"],
            "partition_keys": ["created_date"],
            "required_partition_filter": "created_date >= '2026-01-01'",
        },
        evidence_ids=[evidence_id],
        rationale="Authoritative customer table grain definition.",
    )
    assert proposal["status"] == "pending_review"

    # Decide assertion (approve)
    client.decide_assertion(connection_id, proposal["id"], decision="approve")

    # Certify snapshot
    cert_result = client.certify_snapshot(
        connection_id,
        reviewer="chief_data_steward",
        rationale="All customer assertions verified against enterprise terms.",
    )
    assert cert_result["meaning_certified"] is False
    assert cert_result["agent_ready"] is True
    assert cert_result["contract"]["agent_ready"] is True
    assert cert_result["publication"]["published_by"] == "operator"

    # Verify updated contract has approved grain
    updated_contract = client.get_semantic_contract(connection_id)
    ds = next(d for d in updated_contract["datasets"] if d["asset_id"] == table_asset["id"])
    assert ds["grain"]["key_fields"] == ["customer_id"]
    assert ds["grain"]["partition_keys"] == ["created_date"]
    assert ds["grain"]["required_partition_filter"] == "created_date >= '2026-01-01'"

    # Verify MCP server export
    mcp_server = client.get_mcp_server()
    assert mcp_server is not None
    assert mcp_server.name == "knowledge-layer"

    client.close()


# ---------------------------------------------------------------------------
# 2. Enterprise Catalog Presets & Ingestion Tests
# ---------------------------------------------------------------------------

def _catalog_scope(provider):
    return {
        "provider": provider,
        "source_identity": "offline-fixture",
        "namespace": "test",
        "scope_id": "captured-export",
        "selectors": ["synthetic:all"],
    }


def test_alation_catalog_export_translation():
    raw_alation_tables = [
        {"id": 1001, "name": "dim_customer", "table_type": "TABLE", "description": "Customer dimension table"},
        {"id": 1002, "name": "fact_orders", "table_type": "TABLE", "description": "Order transactions"},
        {"id": 1003, "name": "v_recent_sales", "table_type": "VIEW", "description": "30-day sales view"},
    ]

    package_bytes = translate_catalog_export(
        raw_alation_tables, catalog_format="alation", scope=_catalog_scope("alation"), listing_complete=True,
    )
    package = json.loads(package_bytes.decode("utf-8"))

    assert package["schema_version"] == "knowledge-catalog-export-v1"
    assert package["record_count"] == 3
    assert len(package["records"]) == 3

    r1 = package["records"][0]
    assert r1["native_id"] == "1001"
    assert r1["native_type"] == "Table"
    assert r1["kind"] == "asset"
    assert r1["name"] == "dim_customer"


def test_alation_glossary_term_translation():
    raw_alation_terms = [
        {"id": 501, "title": "Gross Merchandise Value", "description": "Total sales volume prior to deductions"},
        {"id": 502, "title": "Active Customer", "description": "Customer with transaction in last 90 days"},
    ]

    package_bytes = translate_catalog_export(
        raw_alation_terms, catalog_format="alation_terms", scope=_catalog_scope("alation"), listing_complete=True,
    )
    package = json.loads(package_bytes.decode("utf-8"))

    assert package["record_count"] == 2
    r1 = package["records"][0]
    assert r1["native_id"] == "501"
    assert r1["native_type"] == "GlossaryTerm"
    assert r1["kind"] == "semantic_definition"
    assert r1["name"] == "Gross Merchandise Value"


def test_collibra_catalog_export_translation():
    raw_collibra = {
        "total": 3,
        "offset": 0,
        "limit": 100,
        "results": [
            {
                "id": "collibra-uuid-1",
                "name": "Customer Accounts",
                "type": {"name": "Table", "id": "type-1"},
                "status": {"name": "Certified"},
            },
            {
                "id": "collibra-uuid-2",
                "name": "Quarterly Revenue",
                "type": {"name": "Metric", "id": "type-2"},
                "status": {"name": "Approved"},
            },
            {
                "id": "collibra-uuid-3",
                "name": "Churn Rate Policy",
                "type": {"name": "Governance Asset", "id": "type-3"},
                "status": {"name": "Published"},
            },
        ],
    }

    package_bytes = translate_catalog_export(
        raw_collibra, catalog_format="collibra", scope=_catalog_scope("collibra"), listing_complete=True,
    )
    package = json.loads(package_bytes.decode("utf-8"))

    assert package["record_count"] == 3
    records_by_id = {r["native_id"]: r for r in package["records"]}

    r1 = records_by_id["collibra-uuid-1"]
    assert r1["name"] == "Customer Accounts"
    assert r1["kind"] == "asset"
    assert r1["native_type"] == "Table"

    r2 = records_by_id["collibra-uuid-2"]
    assert r2["name"] == "Quarterly Revenue"
    assert r2["kind"] == "semantic_definition"
    assert r2["native_type"] == "Metric"

    r3 = records_by_id["collibra-uuid-3"]
    assert r3["name"] == "Churn Rate Policy"
    assert r3["kind"] == "document"
    assert r3["native_type"] == "Governance Asset"


def test_atlan_catalog_export_translation():
    raw_atlan = {
        "entities": [
            {
                "guid": "atlan-tbl-01",
                "typeName": "Table",
                "attributes": {"name": "snowflake_orders", "connectorName": "snowflake"},
            },
            {
                "guid": "atlan-view-02",
                "typeName": "View",
                "attributes": {"name": "v_daily_active_users", "connectorName": "snowflake"},
            },
            {
                "guid": "atlan-term-03",
                "typeName": "GlossaryTerm",
                "attributes": {"name": "Retention Rate", "shortDescription": "Cohort retention"},
            },
        ],
    }

    package_bytes = translate_catalog_export(
        raw_atlan, catalog_format="atlan", scope=_catalog_scope("atlan"), listing_complete=True,
    )
    package = json.loads(package_bytes.decode("utf-8"))

    assert package["record_count"] == 3
    r_term = next(r for r in package["records"] if r["native_id"] == "atlan-term-03")
    assert r_term["name"] == "Retention Rate"
    assert r_term["kind"] == "semantic_definition"
    assert r_term["native_type"] == "GlossaryTerm"


def test_openmetadata_catalog_export_translation():
    raw_om = {
        "data": [
            {
                "id": "om-uuid-1",
                "name": "raw_events",
                "tableType": "Regular",
                "fullyQualifiedName": "lakehouse.raw_events",
            },
            {
                "id": "om-uuid-2",
                "name": "v_customer_summary",
                "tableType": "View",
                "fullyQualifiedName": "lakehouse.v_customer_summary",
            },
        ],
    }

    package_bytes = translate_catalog_export(
        raw_om, catalog_format="openmetadata", scope=_catalog_scope("openmetadata"), listing_complete=True,
    )
    package = json.loads(package_bytes.decode("utf-8"))

    assert package["record_count"] == 2
    assert package["records"][0]["name"] == "raw_events"
    assert package["records"][0]["kind"] == "asset"


def test_datahub_catalog_export_translation():
    raw_datahub = {
        "entities": [
            {
                "urn": "urn:li:dataset:(urn:li:dataPlatform:bigquery,analytics.sales,PROD)",
                "type": "DATASET",
                "name": "analytics.sales",
            },
            {
                "urn": "urn:li:glossaryTerm:MonthlyActiveUsers",
                "type": "GLOSSARY_TERM",
                "name": "Monthly Active Users",
            },
        ],
    }

    package_bytes = translate_catalog_export(
        raw_datahub, catalog_format="datahub", scope=_catalog_scope("datahub"), listing_complete=True,
    )
    package = json.loads(package_bytes.decode("utf-8"))

    assert package["record_count"] == 2
    r_dataset = package["records"][0]
    assert r_dataset["native_type"] == "DATASET"
    assert r_dataset["kind"] == "asset"

    r_term = package["records"][1]
    assert r_term["native_type"] == "GLOSSARY_TERM"
    assert r_term["kind"] == "semantic_definition"


def test_catalog_preset_retrieval_and_error_handling():
    mapping = get_preset_mapping("alation")
    assert mapping["id_encoding"] == "integer_decimal"
    assert mapping["records_pointer"] == ""

    with pytest.raises(ConnectorError) as exc_info:
        get_preset_mapping("nonexistent_catalog")
    assert exc_info.value.code == "unknown_catalog_format"


# ---------------------------------------------------------------------------
# 3. Iceberg Lakehouse Catalog Profile Builder Tests
# ---------------------------------------------------------------------------

def test_polaris_profile_builder():
    profile = polaris_profile(
        uri="https://polaris.example.com/api/catalog",
        client_id="polaris-client-id",
        client_secret="polaris-client-secret",  # pragma: allowlist secret - offline fixture value
        warehouse="analytics_warehouse",
    )
    assert profile["uri"] == "https://polaris.example.com/api/catalog"
    assert profile["warehouse"] == "analytics_warehouse"
    assert profile["credential"] == "polaris-client-id:polaris-client-secret"  # pragma: allowlist secret - offline fixture value
    assert profile["oauth2-server-uri"] == "https://polaris.example.com/api/catalog/v1/oauth/tokens"
    assert profile["scope"] == "PRINCIPAL_ROLE:ALL"


def test_lakekeeper_profile_builder():
    profile = lakekeeper_profile(
        uri="https://lakekeeper.internal.example/catalog",
        warehouse="gold_lake",
        token="lakekeeper-bearer-token",
    )
    assert profile["uri"] == "https://lakekeeper.internal.example/catalog"
    assert profile["warehouse"] == "gold_lake"
    assert profile["token"] == "lakekeeper-bearer-token"


def test_unity_catalog_profile_builder():
    profile = unity_catalog_profile(
        workspace_url="https://dbc-abcdef-1234.cloud.databricks.com",
        token="dapi1234567890abcdef",
        catalog_name="main_catalog",
    )
    assert profile["uri"] == "https://dbc-abcdef-1234.cloud.databricks.com/api/2.1/unity-catalog/iceberg"
    assert profile["warehouse"] == "main_catalog"
    assert profile["token"] == "dapi1234567890abcdef"


def test_nessie_profile_builder():
    profile = nessie_profile(
        uri="http://localhost:19120/iceberg",
        warehouse="main",
    )
    assert profile["uri"] == "http://localhost:19120/iceberg"
    assert profile["warehouse"] == "main"
