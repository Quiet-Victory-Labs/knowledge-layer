"""Plans load approval and physical tables from the store, never caller flags."""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.planning import metric_plan
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeNotFound, KnowledgeStore
from knowledge_layer.task_context import revalidate_context, task_context


@pytest.fixture
def reviewed_metric(tmp_path, monkeypatch):
    database = tmp_path / "sales.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sales (order_id TEXT, amount REAL, region TEXT)")
        conn.executemany("INSERT INTO sales VALUES (?, ?, ?)", [("A", 10, "east"), ("B", 0, "east"), ("C", None, "west")])
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TEST", "sqlite:///" + str(database))
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", name="Sales", kind="sql_catalog",
                                     config={"provider": "sqlite", "secret_ref": "KNOWLEDGE_SOURCE_TEST"})  # pragma: allowlist secret - public revision/operator reference
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    evidence = next(item["id"] for item in snapshot["evidence"] if item["evidence_type"] == "asset_metadata")
    payload = {"name": "Revenue", "description": "Recorded sale amount in the source unit.",
               "measure_field": "amount", "aggregation": "sum", "unit": "USD",
               "schema_version": "metric-v2", "dimensions": ["region"], "filters": [], "time": None,
               "cohort": {"description": "Sales rows", "key_fields": ["order_id"]},
               "additivity": {"kind": "additive", "non_additive_dimensions": []}}
    proposal = store.submit_contract_assertion(
        "demo", source["id"], assertion_kind="metric", asset_id=asset["id"], payload=payload,
        evidence_ids=[evidence], rationale="Owner reviewed these dimensions, units and row grain.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve")
    snapshot = store.get_snapshot("demo", source["id"])
    store.publish_source("demo", source["id"], expected_snapshot_id=snapshot["id"],
                         expected_snapshot_version=snapshot["version"],
                         rationale="Reviewed revenue definition is ready for bounded analyst planning.")
    yield store, source["id"], proposal["id"], database
    store.close()


def test_plan_executes_exact_fixture_and_reports_missing_separately(reviewed_metric):
    store, cid, pid, database = reviewed_metric
    plan = metric_plan(store, tenant_id="demo", connection_id=cid, assertion_id=pid,
                       request={"group_by": ["region"]})
    assert plan["status"] == "not_executed"
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        rows = [dict(row) for row in conn.execute(plan["sql"], plan["parameters"])]
    assert len(rows) == 2
    # SQL and diagnostics are independently executed here; the plan itself
    # still makes no source execution claim.
    assert sorted(row["metric_value"] for row in rows if row["metric_value"] is not None) == [10]
    assert sum(row["missing_measure_rows"] for row in rows) == 1
    assert sum(row["zero_measure_rows"] for row in rows) == 1


def test_api_and_mcp_use_current_store_and_withdrawal_refuses(reviewed_metric):
    store, cid, pid, _ = reviewed_metric
    with TestClient(build_agent_api(store)) as client:
        response = client.post(f"/sources/{cid}/metric-plan", json={"assertion_id": pid, "request": {}})
        assert response.status_code == 200
        assert response.json()["approval"]["assertion_id"] == pid
    async def run():
        from mcp.server.mcpserver.exceptions import ToolError

        server = build_quick_mcp_server(store)
        result = await server.call_tool("plan_metric_query", {"connection_id": cid, "assertion_id": pid, "request": {}})
        assert not result.is_error
        assert result.structured_content["result_state"] == "not_computed"
        with pytest.raises(ToolError, match="not found"):
            await server.call_tool("plan_metric_query", {"connection_id": cid, "assertion_id": "forged", "request": {}})
    asyncio.run(run())
    store.revoke_publication("demo", cid, reason="Owner withdrew the reviewed metric publication.")
    with pytest.raises(KnowledgeNotFound):
        metric_plan(store, tenant_id="demo", connection_id=cid, assertion_id=pid, request={})


def test_task_receipt_tracks_effective_semantics_and_cannot_be_forged(reviewed_metric):
    from knowledge_layer.store import KnowledgeConflict

    store, cid, _, _ = reviewed_metric
    packet = task_context(store, tenant_id="demo", query="Revenue", connection_ids=[cid])
    assert packet["status"] == "context_available"
    assert packet["answerability"] == "requires_source_execution"
    assert revalidate_context(store, tenant_id="demo", receipt=packet["receipt"])["status"] == "current"
    modified = {**packet["receipt"], "context_digest": "0" * 64}
    with pytest.raises(KnowledgeNotFound):
        revalidate_context(store, tenant_id="demo", receipt=modified)
    store.revoke_publication("demo", cid, reason="Owner withdraws metric while the harness holds a receipt.")
    with pytest.raises(KnowledgeConflict, match="changed"):
        revalidate_context(store, tenant_id="demo", receipt=packet["receipt"])


def test_concurrent_first_receipts_share_one_signing_key(reviewed_metric, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep

    from knowledge_layer import context_receipts as module

    store, cid, _, _ = reviewed_metric
    calls = []
    real_random = module.secrets.token_bytes
    def delayed_random(size):
        calls.append(size)
        sleep(0.02)
        return real_random(size)
    monkeypatch.setattr(module.secrets, "token_bytes", delayed_random)
    barrier = Barrier(4)
    def resolve(_):
        barrier.wait()
        return task_context(store, tenant_id="demo", query="Revenue", connection_ids=[cid])["receipt"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(executor.map(resolve, range(4)))
    assert calls == [32]
    assert all(revalidate_context(store, tenant_id="demo", receipt=receipt)["status"] == "current"
               for receipt in receipts)


def test_fresh_blocked_receipt_refuses_even_when_its_registry_pin_is_unchanged(reviewed_metric):
    from knowledge_layer.store import KnowledgeConflict

    store, cid, _, _ = reviewed_metric
    store.revoke_publication("demo", cid, reason="Owner withdraws the analyst's metric authority.")
    packet = task_context(store, tenant_id="demo", query="Revenue", connection_ids=[cid])
    assert packet["status"] == "blocked"
    assert packet["answerability"] == "not_established"
    with pytest.raises(KnowledgeConflict, match="blocked"):
        revalidate_context(store, tenant_id="demo", receipt=packet["receipt"])
    with TestClient(build_agent_api(store)) as client:
        response = client.post("/task-context/revalidate", json=packet["receipt"])
        assert response.status_code == 409
        assert "blocked" in response.json()["detail"]
    async def verify_mcp_refusal():
        from mcp.server.mcpserver.exceptions import ToolError

        for builder in (build_quick_mcp_server, build_knowledge_mcp_server):
            with pytest.raises(ToolError, match="blocked"):
                await builder(store).call_tool("revalidate_task_context", {"receipt": packet["receipt"]})
    asyncio.run(verify_mcp_refusal())


def test_current_receipt_explicitly_preserves_unknown_answerability(reviewed_metric):
    store, cid, _, _ = reviewed_metric
    packet = task_context(store, tenant_id="demo", query="Unmapped employee retention", connection_ids=[cid])
    assert packet["status"] == "no_direct_match"
    checked = revalidate_context(store, tenant_id="demo", receipt=packet["receipt"])
    assert checked["status"] == "current"
    assert checked["context_status"] == "no_direct_match"
    assert checked["answerability"] == "not_established"
    assert checked["blockers"] == []
    assert checked["source_permissions_verified"] is False
