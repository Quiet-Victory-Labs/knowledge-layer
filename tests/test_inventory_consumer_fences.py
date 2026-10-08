"""Real registry dependency effects during planning and guidance assembly."""

from __future__ import annotations

import importlib.util
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from knowledge_layer.guidance import GuidanceRegistry
from knowledge_layer.planning import metric_plan
from knowledge_layer.store import KnowledgeConflict


def _load_test(name):
    spec = importlib.util.spec_from_file_location(
        f"inventory_consumer_fences_{name}", Path(__file__).with_name(f"{name}.py"),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


citations = _load_test("test_inventory_citations")
planning = _load_test("test_planning_integration")


@pytest.fixture
def consumer_case(tmp_path, monkeypatch):
    # Reuse the actual bounded SQLite SQL connector fixture, rather than
    # inventing a native binding on the local-file source fixture.
    fixture = planning.reviewed_metric.__wrapped__(tmp_path, monkeypatch)
    store, target, original_id, database = next(fixture)
    try:
        catalog = store.create_connection("demo", kind="local_files", name="Catalog capture",
                                          config={"path": "catalog"})
        case = {"store": store, "target": target, "catalog": catalog["id"], "database": database}
        citations.commit_catalog(case)
        snapshot = store.get_snapshot("demo", target)
        original = next(item for item in snapshot["proposals"] if item["id"] == original_id)
        payload = {**deepcopy(original["payload"]), "name": "Net revenue",
                   "description": "Recorded net sale amounts exclude refunded orders."}
        proposal = store.submit_contract_assertion("demo", target, assertion_kind="metric",
            asset_id=original["asset_id"], payload=payload, evidence_ids=original["evidence_ids"],
            evidence_refs=[deepcopy(case["ref"])], expected_snapshot_id=snapshot["id"],
            rationale="The local owner interprets this exact captured term for the sales dataset.")
        store.decide_proposal("demo", target, proposal["id"], decision="approve")
        citations.publish(case)
        case["proposal"] = proposal["id"]
        yield case
    finally:
        with pytest.raises(StopIteration):
            next(fixture)


def _invalidate(case, change):
    if change == "error":
        store = case["store"]
        with store.engine.begin() as conn:
            conn.execute(update(store.connections).where(
                store.connections.c.id == case["catalog"],
            ).values(status="error"))
    else:
        citations.commit_catalog(case, definition="A later capture declares different meaning.",
                                 empty=change == "empty")


def _guidance(case):
    store, target = case["store"], case["target"]
    snapshot = store.get_snapshot("demo", target)
    payload = {"title": "Revenue research procedure", "description": "Use the local reviewed metric.",
        "business_units": [], "tasks": [], "connection_ids": [target],
        "audience": {"company_wide": True, "subjects": [], "groups": []},
        "skills": [{"name": "revenue-research", "description": "Research the reviewed company measure.",
                    "instructions": "Resolve current local meaning before an independently authorized source read.",
                    "required_capabilities": ["knowledge_context"]}],
        "rules": [{"key": "source-writes", "statement": "Connected source writes are forbidden.", "value": False}],
        "dependencies": [{"connection_id": target, "snapshot_id": snapshot["id"],
                          "snapshot_version": snapshot["version"]}]}
    registry = GuidanceRegistry(store)
    for slug in ("a-revenue", "z-revenue"):
        draft = registry.draft("demo", slug, payload)
        registry.publish("demo", slug, revision=draft["revision"], expected_digest=draft["digest"],
                         rationale="The steward reviewed this exact procedure and its current local dependency.")
    return registry, payload


def _audit_count(store):
    with store.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(store.audit_events)).scalar_one()


def test_current_metric_and_guidance_keep_real_sql_effects_without_registry_writes(consumer_case):
    case, store = consumer_case, consumer_case["store"]
    registry, _ = _guidance(case)
    before = _audit_count(store)
    plan = metric_plan(store, tenant_id="demo", connection_id=case["target"],
                       assertion_id=case["proposal"], request={})
    assert plan["status"] == "not_executed" and plan["approval"]["assertion_id"] == case["proposal"]
    with sqlite3.connect(case["database"]) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        row = dict(conn.execute(plan["sql"], plan["parameters"]).fetchone())
    assert row["metric_value"] == 10 and row["missing_measure_rows"] == row["zero_measure_rows"] == 1
    resolved = registry.resolve("demo", connection_ids=[case["target"]])
    assert resolved["status"] == "ready" and len(resolved["bundles"]) == 2
    assert all(item["dependency_state"] == "current" and item["use_as_instructions"]
               for item in resolved["bundles"])
    assert _audit_count(store) == before


@pytest.mark.parametrize("change", ["successor", "empty", "error"])
def test_inventory_change_during_actual_metric_planning_refuses_fixed_snapshot(
    consumer_case, monkeypatch, change,
):
    from knowledge_layer import metric_planner

    case, store = consumer_case, consumer_case["store"]
    before = store.get_snapshot("demo", case["target"])["id"]
    original = metric_planner.plan_metric_query
    assembled = []

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        assembled.append(result)
        _invalidate(case, change)
        return result

    monkeypatch.setattr(metric_planner, "plan_metric_query", changed)
    with pytest.raises(KnowledgeConflict, match="Metric source changed during planning"):
        metric_plan(store, tenant_id="demo", connection_id=case["target"],
                    assertion_id=case["proposal"], request={})
    snapshot = store.get_snapshot("demo", case["target"])
    assert snapshot["id"] == before and not snapshot["agent_ready"]
    assert assembled[0]["approval"]["status"] == "approved" and assembled[0]["sql"]


@pytest.mark.parametrize("change", ["successor", "empty", "error"])
def test_inventory_change_during_later_guidance_bundle_refuses_cached_currentness(
    consumer_case, monkeypatch, change,
):
    case, store = consumer_case, consumer_case["store"]
    registry, _ = _guidance(case)
    before = store.get_snapshot("demo", case["target"])["id"]
    original = registry._dependency_reasons
    visits = []

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        visits.append(result)
        if len(visits) == 2:
            _invalidate(case, change)
        return result

    monkeypatch.setattr(registry, "_dependency_reasons", changed)
    with pytest.raises(KnowledgeConflict, match="Guidance dependencies changed during assembly"):
        registry.resolve("demo", connection_ids=[case["target"]])
    assert visits[:2] == [[], []]
    assert store.get_snapshot("demo", case["target"])["id"] == before


def test_guidance_withdrawn_during_later_bundle_never_returns_old_instructions(consumer_case, monkeypatch):
    case = consumer_case
    registry, _ = _guidance(case)
    original = registry._dependency_reasons
    visits = 0

    def changed(*args, **kwargs):
        nonlocal visits
        result = original(*args, **kwargs)
        visits += 1
        if visits == 2:
            registry.revoke("demo", "a-revenue", revision=1,
                            reason="The steward withdraws this procedure during consumer assembly.")
        return result

    monkeypatch.setattr(registry, "_dependency_reasons", changed)
    with pytest.raises(KnowledgeConflict, match="Guidance publication changed during assembly"):
        registry.resolve("demo", connection_ids=[case["target"]])


def test_new_guidance_draft_during_assembly_preserves_existing_publication(consumer_case, monkeypatch):
    case = consumer_case
    registry, payload = _guidance(case)
    original = registry._dependency_reasons
    visits = 0

    def changed(*args, **kwargs):
        nonlocal visits
        result = original(*args, **kwargs)
        visits += 1
        if visits == 2:
            registry.draft("demo", "a-revenue", {**payload, "description": "A new unpublished draft."},
                           expected_revision=1)
        return result

    monkeypatch.setattr(registry, "_dependency_reasons", changed)
    result = registry.resolve("demo", connection_ids=[case["target"]])
    assert result["status"] == "ready"
    assert {(item["slug"], item["revision"]) for item in result["bundles"]} == {("a-revenue", 1), ("z-revenue", 1)}


@pytest.mark.parametrize("consumer", ["metric", "guidance"])
def test_final_consumers_share_budget_and_propagate_complete_operation_refusal(
    consumer_case, monkeypatch, consumer,
):
    from knowledge_layer import inventory_citations

    case, store = consumer_case, consumer_case["store"]
    registry, _ = _guidance(case)
    # The initial dependency read fits; its final currentness check must share
    # the same operation and refuse rather than reset or suppress the budget.
    monkeypatch.setattr(inventory_citations, "MAX_CITATION_CHECKS", 1)
    with pytest.raises(inventory_citations.InventoryCitationBudgetExceeded, match="revalidation exceeds"):
        if consumer == "metric":
            metric_plan(store, tenant_id="demo", connection_id=case["target"],
                        assertion_id=case["proposal"], request={})
        else:
            registry.resolve("demo", connection_ids=[case["target"]])
