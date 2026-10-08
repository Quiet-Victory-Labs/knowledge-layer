"""Focused coverage for source discovery boundaries and candidate curation."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import pytest

from knowledge_layer import curation

REPO_ROOT = Path(__file__).resolve().parents[1]
CONNECTABLE_ROOT = REPO_ROOT / "data" / "connectable"
TRUTH_PATH = REPO_ROOT / "tests" / "fixtures" / "curation" / "truth_manifest.json"


def _curate(domain: str, **overrides: Any) -> dict[str, Any]:
    config = {
        "root": str(CONNECTABLE_ROOT / domain),
        "source_id": f"{domain}-fixture-source",
        "name": f"{domain.title()} fixture",
    }
    config.update(overrides)
    return curation.curate_source("local_files", config)


def _asset(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    return next(
        asset
        for asset in snapshot["assets"]
        if asset["name"] == name or asset["id"] == name
    )


def _concept(snapshot: dict[str, Any], asset_name: str, field: str) -> dict[str, Any]:
    asset = _asset(snapshot, asset_name)
    return next(
        concept
        for concept in snapshot["concepts"]
        if any(
            provenance.get("asset_id") == asset["id"] and provenance.get("field") == field
            for provenance in concept.get("provenance", [])
        )
    )


def test_northstar_curation_is_stable_candidate_only_and_does_not_retain_rows():
    first = _curate("northstar")
    second = _curate("northstar")

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["status"] == "discovery_complete"
    assert first["discovery_complete"] is True
    assert first["meaning_certified"] is False
    assert first["agent_ready"] is False
    assert first["curation_status"] == "candidate_review_required"
    assert first["capabilities"]["query_execution"] is False
    assert json.loads(json.dumps(first)) == first

    encoded = json.dumps(first, sort_keys=True)
    for source_value in ("WO-4101", "AST-102", "CL-22", "Cedar Ridge Utilities"):
        assert source_value not in encoded

    labor = _concept(first, "work_orders.csv", "labor_hours")
    assert labor["semantic_type"] == "numeric_measure_candidate"
    metric = next(metric for metric in first["metrics"] if metric["field"] == "labor_hours")
    assert metric["unit"] == "hour"
    assert metric["status"] == "candidate"
    assert metric["certified"] is False
    assert metric["grain"]["status"] == "candidate"


def test_northstar_entities_relationship_ambiguity_synonym_and_doc_conflict():
    snapshot = _curate("northstar", retain_semantic_samples=True)
    work_orders = next(
        concept
        for concept in snapshot["concepts"]
        if concept["semantic_type"] == "business_entity" and concept["name"] == "Work Order"
    )
    work_asset = _asset(snapshot, "work_orders.csv")
    assert work_orders["source_binding"]["asset_id"] == work_asset["id"]
    assert work_orders["grain"]["candidate_key_fields"][0]["field"] == "work_order_id"
    assert work_orders["grain"]["candidate_key_fields"][0]["status"] == "unique_in_profile"

    asset_links = [
        relation
        for relation in snapshot["relationships"]
        if relation.get("source_asset_id") == work_asset["id"]
        and relation.get("source_field") == "asset_id"
    ]
    target_names = {_asset(snapshot, relation["target_asset_id"])["name"] for relation in asset_links}
    assert {"active_assets.csv", "retired_assets.csv"} <= target_names
    assert all(relation["status"] == "candidate" for relation in asset_links)
    assert all(relation["ambiguity"] == "multiple_targets" for relation in asset_links)
    assert all(
        proposal["status"] == "pending_review"
        for proposal in snapshot["proposals"]
        if proposal["proposal_type"] == "relationship_mapping"
        and proposal["subject_id"] == work_orders["id"]
    )

    synonym_proposals = [
        proposal
        for proposal in snapshot["proposals"]
        if proposal["proposal_type"] == "possible_synonym"
    ]
    assert synonym_proposals
    assert all(proposal["status"] == "pending_review" for proposal in synonym_proposals)

    conflict = next(
        proposal
        for proposal in snapshot["proposals"]
        if proposal["proposal_type"] == "definition_conflict"
    )
    assert conflict["status"] == "pending_review"
    claims_by_source = {
        claim["source_pointer"]: claim["values"] for claim in conflict["candidate_claims"]
    }
    assert claims_by_source == {
        "sla_policy.md": [{"value": 4.0, "unit": "hour"}],
        "legacy_service_notes.md": [{"value": 8.0, "unit": "hour"}],
    }


def test_juniper_fixture_is_generic_and_hidden_truth_is_outside_connectable_root():
    snapshot = _curate("juniper")
    products = _asset(snapshot, "products.csv")
    orders = _asset(snapshot, "orders.jsonl")
    assert any(
        concept["semantic_type"] == "business_entity"
        and concept["source_binding"]["asset_id"] == products["id"]
        for concept in snapshot["concepts"]
    )
    assert _concept(snapshot, "products.csv", "gtin")["semantic_type"] == "identifier"
    assert _concept(snapshot, "orders.jsonl", "ordered_at")["semantic_type"] == "event_time"
    assert any(
        relation["source_asset_id"] == orders["id"]
        and relation["source_field"] == "product_id"
        and relation["target_asset_id"] == products["id"]
        and relation["status"] == "candidate"
        for relation in snapshot["relationships"]
    )
    assert not snapshot["errors"]
    assert TRUTH_PATH.is_relative_to(REPO_ROOT / "tests")
    assert not TRUTH_PATH.is_relative_to(CONNECTABLE_ROOT)
    assert "not_for_agent_or_customer_api" in TRUTH_PATH.read_text(encoding="utf-8")


def test_local_file_drift_reports_asset_schema_change(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    table = root / "jobs.csv"
    table.write_text("job_id,minutes\n1,12\n2,8\n", encoding="utf-8")
    config = {"root": str(root), "source_id": "drift-source"}
    original = curation.curate_source("local_files", config)

    table.write_text("job_id,minutes,priority\n1,12,high\n2,8,low\n", encoding="utf-8")
    changed = curation.curate_source(
        "local_files", {**config, "previous_snapshot": original}
    )

    assert changed["discovery_complete"] is True
    assert changed["drift"]["previous_snapshot_id"] == original["snapshot_id"]
    assert changed["drift"]["changed_assets"][0]["change_types"] == [
        "revision_changed",
        "schema_changed",
        "profile_changed",
    ]


def test_local_files_rejects_escape_and_symlink_paths(tmp_path: Path):
    root = tmp_path / "visible"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.csv").write_text("id\nsecret\n", encoding="utf-8")

    traversal = curation.curate_source(
        "local_files", {"root": str(root), "path": "../outside"}
    )
    assert traversal["errors"][0]["code"] == "path_outside_root"
    assert not traversal["assets"]

    link = root / "linked"
    link.symlink_to(outside, target_is_directory=True)
    linked = curation.curate_source(
        "local_files", {"root": str(root), "path": "linked"}
    )
    assert linked["errors"][0]["code"] == "path_outside_root"
    assert "secret" not in json.dumps(linked)


def test_iceberg_connector_enforces_tenant_catalog_allowlist(monkeypatch: pytest.MonkeyPatch):
    def forbidden_load_catalog(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("unallowlisted catalog must be rejected before connecting")

    monkeypatch.setattr(curation.iceberg, "_BoundedRestCatalog", forbidden_load_catalog)
    monkeypatch.setenv("KNOWLEDGE_ICEBERG_CATALOGS", "juniper")
    result = curation.curate_source(
        "iceberg", {"catalog": "other_tenant", "namespace": "public"}
    )
    assert result["errors"][0]["code"] == "catalog_not_allowlisted"
    assert result["agent_ready"] is False


@pytest.mark.parametrize("change", ["unchanged", "snapshot", "schema_id", "field_id", "physical_type", "nested_field_id", "table_uuid"])
def test_iceberg_connector_returns_metadata_only_candidates(
    monkeypatch: pytest.MonkeyPatch, change: str
):
    from pyiceberg.schema import Schema
    from pyiceberg.types import IntegerType, LongType, NestedField, StructType

    class Snapshot:
        snapshot_id = 17
        summary: ClassVar[dict[str, str]] = {"total-records": "3", "total-data-files": "1"}

    observed_schema = Schema(
        NestedField(1, "job_id", IntegerType(), required=True),
        NestedField(2, "metrics", StructType(NestedField(3, "count", IntegerType()))),
        schema_id=2,
    )

    class Table:
        io = SimpleNamespace(properties={})
        metadata = SimpleNamespace(table_uuid="00000000-0000-0000-0000-000000000001")

        @staticmethod
        def schema() -> Schema:
            return observed_schema

        @staticmethod
        def current_snapshot() -> Snapshot:
            return Snapshot()

    class Catalog:
        @staticmethod
        def namespace_exists(namespace: str) -> bool:
            return namespace == "service"

        @staticmethod
        def list_tables(_namespace: str) -> list[tuple[str, str]]:
            return [("service", "work_orders")]

        @staticmethod
        def load_table(identifier: str) -> Table:
            assert identifier == "service.work_orders"
            return Table()

    monkeypatch.setenv("KNOWLEDGE_ICEBERG_CATALOGS", "tenant_catalog")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(curation.iceberg, "_client_credentials", lambda: ("client", "secret"))
    monkeypatch.setattr(curation.iceberg, "_BoundedRestCatalog", lambda *_args, **_kwargs: Catalog())

    result = curation.curate_source(
        "iceberg",
        {"catalog": "tenant_catalog", "namespace": "service", "source_id": "stable-id"},
    )
    assert result["discovery_complete"] is True
    assert result["assets"][0]["profile"]["row_count"] == 3
    assert result["capabilities"]["safe_profiling"] is False
    assert result["capabilities"]["query_execution"] is False
    assert result["meaning_certified"] is False
    assert result["assets"][0]["schema"][1]["data_type"] == "structured"

    if change == "snapshot":
        Snapshot.snapshot_id = 18
    elif change == "table_uuid":
        Table.metadata.table_uuid = "00000000-0000-0000-0000-000000000002"
    elif change != "unchanged":
        observed_schema = Schema(
            NestedField(4 if change == "field_id" else 1, "job_id",
                        LongType() if change == "physical_type" else IntegerType(), required=True),
            NestedField(2, "metrics", StructType(
                NestedField(5 if change == "nested_field_id" else 3, "count", IntegerType())
            )),
            schema_id=3 if change == "schema_id" else 2,
        )
    refreshed = curation.curate_source(
        "iceberg", {"catalog": "tenant_catalog", "namespace": "service", "source_id": "stable-id",
                    "previous_snapshot": result},
    )
    # These changes share the same normalized display schema. Native schema
    # identity and data snapshot identity must each still invalidate evidence.
    def normalize(schema):
        return [{key: field[key] for key in ("name", "data_type", "nullable")} for field in schema]
    assert normalize(refreshed["assets"][0]["schema"]) == normalize(result["assets"][0]["schema"])
    assert refreshed["assets"][0]["schema"][0]["field_id"] == (4 if change == "field_id" else 1)
    assert refreshed["assets"][0]["profile"]["schema_id"] == (3 if change == "schema_id" else 2)
    assert refreshed["assets"][0]["profile"]["table_uuid"].endswith("2" if change == "table_uuid" else "1")
    if change == "unchanged":
        assert refreshed["drift"]["changed_assets"] == []
        assert refreshed["snapshot_id"] == result["snapshot_id"]
    else:
        assert refreshed["assets"][0]["revision"] != result["assets"][0]["revision"]
        expected = ["revision_changed"]
        if change in {"schema_id", "table_uuid", "snapshot"}:
            expected.append("profile_changed")
        elif change != "snapshot":
            expected.append("schema_changed")
        assert refreshed["drift"]["changed_assets"][0]["change_types"] == expected
    assert refreshed["assets"][0]["profile"]["metadata_only"] is True


@pytest.mark.parametrize(
    ("catalog_estimate", "expected_estimate", "estimate_state"),
    [
        (-1.0, None, "unknown"),
        (None, None, "unknown"),
        (0.0, 0, "estimated"),
        (12.0, 12, "estimated"),
    ],
)
def test_postgres_connector_uses_read_only_metadata_queries(
    monkeypatch: pytest.MonkeyPatch,
    catalog_estimate: float | None,
    expected_estimate: int | None,
    estimate_state: str,
):
    calls: list[tuple[str, Any]] = []

    class Cursor:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, params: Any = None) -> None:
            calls.append((statement, params))

        def fetchall(self) -> list[tuple[Any, ...]]:
            statement = calls[-1][0]
            if "information_schema" in statement:
                return [("work_orders", "job_id", "bigint", "NO", "bigint")]
            if "pg_catalog.pg_class" in statement:
                return [("work_orders", catalog_estimate)]
            return []

    class Connection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def cursor() -> Cursor:
            return Cursor()

    connect_args: list[tuple[Any, dict[str, Any]]] = []

    def connect(dsn: str, **kwargs: Any) -> Connection:
        connect_args.append((dsn, kwargs))
        return Connection()

    monkeypatch.setenv("NORTHSTAR_DB_SECRET", "postgresql://user:secret@db/customer")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    result = curation.curate_source(
        "postgres", {"secret_ref": "NORTHSTAR_DB_SECRET", "schema": "service"}
    )

    assert result["discovery_complete"] is True
    profile = result["assets"][0]["profile"]
    assert profile["row_count_estimate"] == expected_estimate
    assert profile["row_count_estimate_state"] == estimate_state
    assert profile["row_count"] is None
    assert profile["row_count_is_complete"] is False
    assert profile["profile_rows"] == 0
    assert connect_args[0][0] == "postgresql://user:secret@db/customer"
    assert "default_transaction_read_only=on" in connect_args[0][1]["options"]
    assert any("information_schema" in statement for statement, _params in calls)
    assert any("pg_catalog.pg_class" in statement for statement, _params in calls)
    assert all("SELECT *" not in statement for statement, _params in calls)
    assert "postgresql://user:secret" not in json.dumps(result)
    assert result["capabilities"]["query_execution"] is False


def test_postgres_does_not_echo_missing_secret_and_unknown_kind_is_explicit():
    missing_secret = curation.curate_source(
        "postgres", {"secret_ref": "MISSING_DB_PASSWORD"}
    )
    unsupported = curation.curate_source("spreadsheet", {})
    assert missing_secret["errors"][0]["code"] == "database_credentials_unavailable"
    assert "MISSING_DB_PASSWORD" not in json.dumps(missing_secret)
    assert unsupported["errors"][0]["code"] == "unsupported_connector"


@pytest.mark.parametrize(
    ("before_type", "before_physical", "after_type", "after_physical"),
    [
        ("integer", "integer", "bigint", "bigint"),
        ("numeric", "numeric(10,2)", "numeric", "numeric(10,3)"),
        ("numeric", "numeric(10,2)", "numeric", "numeric(12,2)"),
        ("timestamp without time zone", "timestamp without time zone",
         "timestamp with time zone", "timestamp with time zone"),
        ("timestamp without time zone", "timestamp(3) without time zone",
         "timestamp without time zone", "timestamp(6) without time zone"),
    ],
)
def test_postgres_physical_type_drift_survives_normalized_schema(
    monkeypatch: pytest.MonkeyPatch, before_type: str, before_physical: str,
    after_type: str, after_physical: str,
):
    state = {"type": before_type, "physical": before_physical, "statement": ""}

    def execute(statement: str, _params: Any = None) -> None:
        state["statement"] = statement

    def fetchall() -> list[tuple[Any, ...]]:
        if "information_schema" in state["statement"]:
            return [("events", "value", state["type"], "YES", state["physical"])]
        return [("events", -1.0)]

    cursor = SimpleNamespace(execute=execute, fetchall=fetchall)
    connection = SimpleNamespace(cursor=lambda: nullcontext(cursor))
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(
        connect=lambda *_args, **_kwargs: nullcontext(connection)
    ))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST_POSTGRES", "postgresql://source/example")
    config = {"secret_ref": "KNOWLEDGE_SOURCE_TEST_POSTGRES", "schema": "public", "source_id": "stable-id"}  # pragma: allowlist secret - operator reference
    first = curation.curate_source("postgres", config)
    unchanged = curation.curate_source("postgres", {**config, "previous_snapshot": first})
    assert unchanged["drift"]["changed_assets"] == []
    state.update(type=after_type, physical=after_physical)
    changed = curation.curate_source("postgres", {**config, "previous_snapshot": first})
    assert changed["discovery_complete"] is True
    def normalize(schema):
        return [{key: field[key] for key in ("name", "data_type", "nullable")} for field in schema]
    assert normalize(changed["assets"][0]["schema"]) == normalize(first["assets"][0]["schema"])
    assert changed["assets"][0]["schema"][0]["native_type"] == after_physical
    assert changed["assets"][0]["revision"] != first["assets"][0]["revision"]
    assert changed["drift"]["changed_assets"][0]["change_types"] == ["revision_changed", "schema_changed"]
    assert changed["assets"][0]["profile"]["row_count"] is None
    assert changed["assets"][0]["profile"]["profile_rows"] == 0


def test_drift_detects_added_and_removed_files(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    first = root / "first.csv"
    first.write_text("id\n1\n", encoding="utf-8")
    original = curation.curate_source("local_files", {"root": str(root), "source_id": "source"})
    first.unlink()
    (root / "second.csv").write_text("id\n2\n", encoding="utf-8")
    changed = curation.curate_source(
        "local_files",
        {"root": str(root), "source_id": "source", "previous_snapshot": original},
    )
    assert len(changed["drift"]["added_assets"]) == 1
    assert len(changed["drift"]["removed_assets"]) == 1


def test_semantic_sampling_and_incremental_reprofiling_keep_review_boundaries(tmp_path: Path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "indicators.jsonl").write_text(
        '{"indicator_id":"POP","indicator_name":"Population"}\n'
        '{"indicator_id":"GDP","indicator_name":"GDP"}\n', encoding="utf-8"
    )
    observations = root / "observations.jsonl"
    observations.write_text(
        '{"indicator_id":"POP","year":2023,"value":10}\n'
        '{"indicator_id":"POP","year":2024,"value":11}\n'
        '{"indicator_id":"GDP","year":2024,"value":20}\n', encoding="utf-8"
    )
    (root / "entities.jsonl").write_text(
        '{"entity_id":"Q1","label":"Earth"}\n'
        '{"entity_id":"Q2","label":"Solar System"}\n', encoding="utf-8"
    )
    (root / "edges.jsonl").write_text(
        '{"subject_id":"Q1","predicate":"part_of","parent_id":"Q2"}\n', encoding="utf-8"
    )
    (root / "notes.md").write_text(
        "# Population\n\nPopulation counts residents at the reference date.\n", encoding="utf-8"
    )
    base = {"root": str(root), "source_id": "public-source"}
    private = curation.curate_source("local_files", base)
    assert "Population counts residents" not in json.dumps(private)
    assert "Earth" not in json.dumps(private)
    assert not any(item.get("predicate") == "part_of" for item in private["relationships"])

    config = {**base, "retain_semantic_samples": True}
    first = curation.curate_source("local_files", {**config, "previous_snapshot": private})
    assert len(first["profiling"]["reprofiled_assets"]) == 5
    assert len(first["profiling"]["reused_assets"]) == 0
    assert first["meaning_certified"] is False
    sampled_asset_ids = {asset["id"] for asset in first["assets"] if asset["profile"].get("content_retained")}
    assert sampled_asset_ids
    assert all(
        evidence["details"]["source_values_retained"] is True
        for evidence in first["evidence"]
        if evidence["asset_id"] in sampled_asset_ids and evidence["evidence_type"] == "asset_metadata"
    )
    links = [item for item in first["relationships"] if item["predicate"] == "may_reference_entity"]
    assert len(links) == 1
    assert next(asset for asset in first["assets"] if asset["id"] == links[0]["source_asset_id"])["name"] == "observations.jsonl"
    assert next(asset for asset in first["assets"] if asset["id"] == links[0]["target_asset_id"])["name"] == "indicators.jsonl"
    assert any(item["field"] == "value" and item["status"] == "candidate" for item in first["metrics"])
    asserted = next(item for item in first["relationships"] if item["predicate"] == "part_of")
    assert asserted["status"] == "observed_source_assertion" and asserted["certified"] is False
    assert set(asserted["evidence_ids"]) <= {item["id"] for item in first["evidence"]}
    assert any(
        definition.get("text", "").startswith("Population counts residents")
        for concept in first["concepts"] for definition in concept.get("candidate_definitions", [])
    )

    second = curation.curate_source("local_files", {**config, "previous_snapshot": first})
    assert second["snapshot_id"] == first["snapshot_id"]
    assert len(second["profiling"]["reused_assets"]) == 5
    assert second["profiling"]["reprofiled_assets"] == []
    assert not second["drift"]["changed_assets"]

    observations.write_text(observations.read_text(encoding="utf-8") + '{"indicator_id":"GDP","year":2025,"value":21}\n', encoding="utf-8")
    third = curation.curate_source("local_files", {**config, "previous_snapshot": second})
    assert third["profiling"]["reprofiled_assets"] == ["observations.jsonl"]
    assert len(third["profiling"]["reused_assets"]) == 4
    assert len(third["drift"]["changed_assets"]) == 1
