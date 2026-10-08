"""Composite planning is reachable through existing reviewed public contracts."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.contracts import ossie_preview, semantic_contract
from knowledge_layer.planning import metric_plan
from knowledge_layer.store import KnowledgeNotFound, KnowledgeStore, KnowledgeValidationError


@pytest.fixture
def reviewed_composite(tmp_path, monkeypatch):
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sales(id TEXT, amount REAL, expense REAL, region TEXT)")
        conn.executemany("INSERT INTO sales VALUES(?,?,?,?)", [
            ("a", 12, 4, "east"), ("b", 9, 6, "east"), ("c", None, 100, "east"), ("d", 0, 0, "west")])
    monkeypatch.setenv("KNOWLEDGE_SOURCE_COMPOSITE", "sqlite:///" + str(database))
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="sql_catalog", name="Composite sales",
        config={"provider": "sqlite", "secret_ref": "KNOWLEDGE_SOURCE_COMPOSITE"})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    handle = next(item["id"] for item in snapshot["evidence"] if item["evidence_type"] == "asset_metadata")
    grain = store.submit_contract_assertion("demo", source["id"], assertion_kind="grain", asset_id=asset["id"],
        payload={"description": "One sale per source id.", "key_fields": ["id"]}, evidence_ids=[handle],
        rationale="Owner reviewed the source event grain.", expected_snapshot_id=snapshot["id"])
    store.decide_proposal("demo", source["id"], grain["id"], decision="approve")
    payload = {"schema_version": "metric-composite-v1", "name": "Profit margin", "description": "Aligned aggregate profit margin.",
        "measure_field": "amount", "aggregation": "composite", "unit": "ratio", "dimensions": ["region"],
        "filters": [], "time": None, "cohort": {"description": "One sale per source id.", "key_fields": ["id"]},
        "additivity": {"kind": "non_additive", "non_additive_dimensions": []}, "null_policy": "complete_case", "zero_division": "null",
        "measures": [{"alias": "revenue", "measure_field": "amount", "aggregation": "sum", "unit": "USD"},
            {"alias": "cost", "measure_field": "expense", "aggregation": "sum", "unit": "USD"}],
        "expression": {"op": "divide", "left": {"op": "subtract", "left": {"op": "measure", "alias": "revenue"},
            "right": {"op": "measure", "alias": "cost"}}, "right": {"op": "measure", "alias": "revenue"}}}
    proposal = store.submit_contract_assertion("demo", source["id"], assertion_kind="metric", asset_id=asset["id"],
        payload=payload, evidence_ids=[handle], rationale="Owner reviewed the shared scope, measures and formula.",
        expected_snapshot_id=store.get_snapshot("demo", source["id"])["id"])
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve")
    snapshot = store.get_snapshot("demo", source["id"])
    store.publish_source("demo", source["id"], expected_snapshot_id=snapshot["id"], expected_snapshot_version=snapshot["version"],
        rationale="Owner approved this composite for bounded metric planning.")
    yield store, source["id"], proposal["id"], grain["id"], database
    store.close()


def test_public_plan_and_contract_preserve_composite_formula_and_real_effect(reviewed_composite):
    store, cid, pid, _grain, database = reviewed_composite
    contract = semantic_contract(store, tenant_id="demo", connection_id=cid)
    metric = next(item for item in contract["metrics"] if item["decision_proposal_id"] == pid)
    assert metric["semantics"]["schema_version"] == "metric-composite-v1"
    assert metric["query_guidance"]["planning_tool"] == "plan_metric_query"
    with TestClient(build_agent_api(store)) as client:
        response = client.post(f"/sources/{cid}/metric-plan", json={"assertion_id": pid, "request": {"group_by": ["region"]}})
    assert response.status_code == 200
    plan = response.json()
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        result = [dict(row) for row in conn.execute(plan["sql"], plan["parameters"])]
    assert result[0]["metric_value"] == pytest.approx(11 / 21)
    assert result[0]["missing_measure_rows"] == 1 and result[1]["metric_value"] is None
    exported = ossie_preview(store, tenant_id="demo", connection_id=cid)
    assert exported["lossless"] is False
    assert any(item["metric_id"] == metric["id"] for item in exported["omitted"])
    assert exported["model"]["metrics"] == []


def test_public_composite_requires_current_reviewed_grain_and_publication(reviewed_composite):
    store, cid, pid, grain, _database = reviewed_composite
    store.revoke_publication("demo", cid, reason="Owner withdraws the metric publication.")
    with pytest.raises(KnowledgeNotFound):
        metric_plan(store, tenant_id="demo", connection_id=cid, assertion_id=pid, request={})
    store.archive_proposals("demo", cid, proposal_ids=[grain], reason="Owner withdraws the source grain assertion.")
    # Republishing the reviewed metric cannot fabricate a grain approval.
    snapshot = store.refresh_connection("demo", cid)["snapshot"]
    store.publish_source("demo", cid, expected_snapshot_id=snapshot["id"], expected_snapshot_version=snapshot["version"],
        rationale="Metric publication alone does not replace the archived source grain.")
    with pytest.raises(KnowledgeValidationError, match="approved exact cohort grain"):
        metric_plan(store, tenant_id="demo", connection_id=cid, assertion_id=pid, request={})
