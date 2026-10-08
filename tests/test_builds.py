"""Durable work survives restarts, commits atomically, and never bypasses review."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.builds import BuildRuns
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
)


@pytest.fixture(params=["file", "memory"])
def setup(tmp_path, request):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,15\n")
    url = "sqlite:///:memory:" if request.param == "memory" else f"sqlite:///{tmp_path / 'db'}"
    store = KnowledgeStore(database_url=url, local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    yield store, source["id"], snapshot
    store.close()


def proposal(task):
    packet = task["packet"]
    result = {"action": "propose", "reason": "Observed metadata supports this bounded source definition.",
              "evidence_ids": packet["evidence_ids"]}
    if packet["track"] == "meaning":
        if packet["kind"] == "review_relationship":
            result["proposed_mapping"] = packet["candidate"]["proposed_mapping_shape"]
        else:
            result["proposed_definition"] = "A term observed in the source, with scope limited to this dataset."
    elif packet["kind"] == "dataset_description":
        result["payload"] = {"description": "Observed source order records."}
    elif packet["kind"] == "field_description":
        result["payload"] = {"field": packet["candidate"]["field"], "description": "An observed source field."}
    else:
        result = {"action": "abstain", "reason": "Evidence needs owner clarification before a business assertion."}
    return result


def test_atomic_checkpoint_idempotency_and_restart(setup):
    store, cid, snapshot = setup
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    assert run["coverage"]["complete"] is True
    assert {item["track"] for item in run["tasks"]} == {"meaning", "contract"}
    task = runs.claim("demo", run["id"])["task"]
    packet = task["packet"]
    evidence = {item["id"]: item for item in snapshot["evidence"]}
    assert packet["evidence"] == [evidence[eid] for eid in packet["evidence_ids"]]
    kwargs = {"task_id": task["id"], "lease_token": task["lease_token"], "result": proposal(task)}
    result = runs.complete("demo", run["id"], **kwargs)
    assert result["status"] == "proposed"
    restarted = BuildRuns(store)
    replay = restarted.complete("demo", run["id"], **kwargs)
    assert replay["replayed"] and replay["proposal_id"] == result["proposal_id"]
    current = restarted.get("demo", run["id"])
    assert current["counts"]["proposed"] == 1
    assert store.get_snapshot("demo", cid)["agent_ready"] is False
    assert store.get_snapshot("demo", cid)["version"] == snapshot["version"]
    assert next(p for p in store.get_snapshot("demo", cid)["proposals"] if p["id"] == result["proposal_id"])["status"] == "pending_review"
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.decisions)).scalar_one() == 0
    with pytest.raises(KnowledgeConflict, match="different result"):
        restarted.complete("demo", run["id"], **{**kwargs, "result": {"action": "defer", "reason": "A different outcome cannot overwrite a receipt."}})


def test_failed_checkpoint_rolls_back_proposal(setup, monkeypatch):
    store, cid, _ = setup
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    task = runs.claim("demo", run["id"])["task"]
    original = store._audit
    def fail(conn, tenant, source, kind, actor, details):
        if kind == "build.checkpoint":
            raise RuntimeError("checkpoint storage failure")
        return original(conn, tenant, source, kind, actor, details)
    monkeypatch.setattr(store, "_audit", fail)
    with pytest.raises(RuntimeError, match="storage failure"):
        runs.complete("demo", run["id"], task_id=task["id"], lease_token=task["lease_token"], result=proposal(task))
    assert not [p for p in store.get_snapshot("demo", cid)["proposals"] if p.get("proposal_type") in {"semantic_correction", "contract_assertion"}]
    assert runs.get("demo", run["id"])["counts"]["leased"] == 1


def test_expired_lease_reclaimed_and_old_owner_refused(setup):
    store, cid, _ = setup
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    task = runs.claim("demo", run["id"])["task"]
    with store.engine.begin() as conn:
        conn.execute(update(store.build_tasks).where(store.build_tasks.c.id == task["id"]).values(lease_until="2000-01-01T00:00:00Z"))
    reclaimed = runs.claim("demo", run["id"])["task"]
    # Fair queue order may choose another pending task; every old lease is invalid regardless.
    assert reclaimed["lease_token"] != task["lease_token"]
    with pytest.raises(KnowledgeConflict):
        runs.complete("demo", run["id"], task_id=task["id"], lease_token=task["lease_token"], result=proposal(task))


def test_changed_snapshot_invalidates_leases_and_regenerates(setup):
    store, cid, _ = setup
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid])
    task = runs.claim("demo", run["id"])["task"]
    store.refresh_connection("demo", cid)
    with pytest.raises(KnowledgeConflict, match="snapshot changed"):
        runs.complete("demo", run["id"], task_id=task["id"], lease_token=task["lease_token"], result=proposal(task))
    fresh = runs.get("demo", run["id"])
    assert fresh["counts"]["stale"] > 0 and fresh["counts"]["pending"] > 0
    assert fresh["sources"][0]["snapshot_version"] != task["packet"]["snapshot_version"]


def test_cancel_and_expired_budget_refuse_agent_writes(setup):
    store, cid, _ = setup
    runs = BuildRuns(store)
    for expire in (False, True):
        run = runs.start("demo", connection_ids=[cid])
        task = runs.claim("demo", run["id"])["task"]
        if expire:
            with store.engine.begin() as conn:
                conn.execute(update(store.builds).where(store.builds.c.id == run["id"]).values(expires_at="2000-01-01T00:00:00Z"))
        else:
            assert runs.cancel("demo", run["id"], reason="The user cancelled this source build.")["status"] == "cancelled"
        with pytest.raises(KnowledgeConflict):
            runs.complete("demo", run["id"], task_id=task["id"], lease_token=task["lease_token"], result=proposal(task))
        current = runs.get("demo", run["id"])
        assert current["status"] == ("budget_exhausted" if expire else "cancelled")
        assert runs.claim("demo", run["id"])["task"] is None


def test_item_budget_is_visible_after_drain_and_abstention_persists(setup):
    store, cid, _ = setup
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[cid], max_items=1)
    assert run["coverage"]["complete"] is False and run["counts"]["total"] == 1
    task = runs.claim("demo", run["id"])["task"]
    runs.complete("demo", run["id"], task_id=task["id"], lease_token=task["lease_token"],
                  result={"action": "abstain", "reason": "Evidence is insufficient for this business definition."})
    current = BuildRuns(store).get("demo", run["id"])
    assert current["status"] == "budget_exhausted" and current["counts"]["abstained"] == 1


def test_waiting_discovery_resumes(setup):
    store, _, _ = setup
    source = store.create_connection("demo", name="Not yet scanned", kind="local_files", config={"path": "."})
    runs = BuildRuns(store)
    run = runs.start("demo", connection_ids=[source["id"]])
    assert run["status"] == "waiting_for_discovery"
    store.refresh_connection("demo", source["id"])
    assert runs.get("demo", run["id"])["status"] == "queued"


def test_read_grants_revocation_and_agent_identity(setup):
    store, cid, _ = setup
    owner = Principal("owner", frozenset())
    reader = Principal("reader", frozenset())
    runs = BuildRuns(store)
    store.access_enforced = True
    with store.engine.begin() as conn:
        conn.execute(store.source_grants.insert().values(tenant_id="demo", connection_id=cid,
            principal_type="user", principal_id="owner", role="manage", granted_at="2026-10-04T00:00:00Z", granted_by="operator"))
    with principal_scope(owner):
        run = runs.start("demo", connection_ids=[cid])
        task = runs.claim("demo", run["id"])["task"]
    with principal_scope(reader):
        assert runs.list("demo") == []
        with pytest.raises(KnowledgeNotFound):
            runs.get("demo", run["id"])
    with store.engine.begin() as conn:
        conn.execute(store.source_grants.delete().where(store.source_grants.c.connection_id == cid))
    with principal_scope(owner), pytest.raises(KnowledgeNotFound):
        runs.complete("demo", run["id"], task_id=task["id"], lease_token=task["lease_token"], result=proposal(task))


def test_transport_matches_browser_but_only_agents_claim(setup):
    store, cid, _ = setup
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store))
    with TestClient(app) as browser, TestClient(build_agent_api(store)) as agent:
        bad = browser.post("/api/knowledge/builds", json={"connection_ids": [cid], "max_items": True})
        assert bad.status_code == 422
        run = browser.post("/api/knowledge/builds", json={"connection_ids": [cid]}).json()["build"]
        assert browser.post(f"/api/knowledge/builds/{run['id']}/claim", json={}).status_code == 404
        task = agent.post(f"/builds/{run['id']}/claim", json={}).json()["task"]
        result = agent.post(f"/builds/{run['id']}/complete", json={"task_id": task["id"],
            "lease_token": task["lease_token"], "result": proposal(task)})
        assert result.status_code == 200 and result.json()["status"] == "proposed"
        assert browser.get(f"/api/knowledge/builds/{run['id']}").json()["build"]["counts"]["proposed"] == 1


def test_mcp_has_strict_operations_and_no_approval(setup):
    import asyncio
    store, cid, _ = setup
    server = build_quick_mcp_server(store)
    async def run():
        result = await server.call_tool("build_knowledge", {"operation": "start", "arguments": {"connection_ids": [cid]}})
        run_id = result.structured_content["build"]["id"]
        claimed = await server.call_tool("build_knowledge", {"operation": "claim", "arguments": {"run_id": run_id}})
        assert claimed.structured_content["task"]["packet"]["evidence"]
        from mcp.server.mcpserver.exceptions import ToolError
        with pytest.raises(ToolError):
            await server.call_tool("build_knowledge", {"operation": "approve", "arguments": {"run_id": run_id}})
    asyncio.run(run())
