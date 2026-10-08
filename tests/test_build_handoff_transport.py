"""A customer's harness gets exact bounded build work without dispatch or writes."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import delete, select

from knowledge_layer import build_handoff as handoff_module
from knowledge_layer import mcp as mcp_module
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.build_handoff import build_handoff
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore


def _payload(name="Orders", *, concepts=1, summary=None):
    return {
        "snapshot_id": f"snapshot-{name}", "status": "discovery_complete", "discovery_complete": True,
        "curation_complete": True,
        "assets": [{"id": "asset-orders", "name": "Orders", "asset_type": "table", "schema": [
            {"name": "order_id", "data_type": "text"}, {"name": "amount", "data_type": "number"},
        ]}],
        "concepts": [{"id": f"concept-{index}", "name": f"Order term {index}", "semantic_type": "source_entity",
                      "status": "candidate", "evidence_ids": ["metadata"], "provenance": [{"asset_id": "asset-orders"}]}
                     for index in range(concepts)],
        "evidence": [{"id": "metadata", "asset_id": "asset-orders", "evidence_type": "asset_metadata",
                      "summary": summary or "Exact stored evidence: " + ("注文 完整 definition\n" * 100),
                      "details": {"scope": "customer orders", "source_revision": "native-v3"}},
                     *[{"id": field, "asset_id": "asset-orders", "evidence_type": "field_metadata",
                        "summary": "Observed exact source field", "details": {"field": field}}
                       for field in ("order_id", "amount")]],
        "proposals": [], "relationships": [], "metrics": [],
        "drift": {"added_assets": [], "changed_assets": [], "removed_assets": []},
    }


def _source(store, name="Orders", **kwargs):
    source = store.create_connection("demo", kind="local_files", name=name, config={})
    snapshot = store.save_snapshot("demo", source["id"], _payload(name, **kwargs), source_refresh=True)
    return source["id"], snapshot


@pytest.fixture
def registry(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'build.db'}")
    source_id, snapshot = _source(store)
    yield store, source_id, snapshot
    store.close()


def _writes(store):
    with store.engine.connect() as conn:
        return {
            name: [dict(row) for row in conn.execute(select(getattr(store, name))).mappings()]
            for name in ("snapshots", "proposals", "decisions", "refresh_jobs")
        }


def _assert_exact(result, source_id, snapshot):
    assert result["schema_version"] == "knowledge-build-handoff-v1"
    assert result["derived_only"] is True and result["source_reads_executed"] is False
    assert result["harness"]["agent_dispatched"] is False
    assert result["harness"]["model_invoked"] is False
    assert result["harness"]["build_run_created"] is False
    source = result["sources"][0]
    assert source["build_state"] == "draft_work_available"
    assert source["human_acceptance"]["agent_can_accept"] is False
    assert source["human_acceptance"]["expected_snapshot_id"] == snapshot["id"]
    evidence = {row["id"]: row for row in snapshot["evidence"]}
    for track in ("meaning", "contract"):
        work = source["interpretation"][track]
        assert work["sample_only"] is True
        assert work["navigation"]["arguments"] == {
            "connection_id": source_id, "track": track, "limit": 3, "offset": 0,
        }
        assert work["work"]
        for packet in work["work"]:
            assert packet["expected_snapshot_id"] == snapshot["id"]
            assert packet["snapshot_version"] == snapshot["version"]
            assert packet["submission_tool"] == ("propose_meaning" if track == "meaning" else "propose_contract")
            assert packet["evidence_text_complete"] is True
            assert packet["evidence"] == [evidence[handle] for handle in packet["evidence_ids"]]
            assert packet["local_evidence_refs"] == [{
                "connection_id": source_id, "snapshot_id": snapshot["id"], "evidence_id": handle,
            } for handle in packet["evidence_ids"]]


@pytest.mark.parametrize("source_management,autonomy", [(False, False), (True, False), (False, True), (True, True)])
def test_http_and_compact_mcp_keep_exact_evidence_without_feature_flags_or_mutations(
    registry, monkeypatch, source_management, autonomy,
):
    store, source_id, snapshot = registry
    before = _writes(store)
    monkeypatch.setattr(store, "refresh_connection", lambda *_args, **_kwargs: pytest.fail("connected source read"))
    monkeypatch.setattr(store, "get_concepts", lambda *_args, **_kwargs: pytest.fail("unscoped concept scan"))
    config = {"enable_source_management": source_management, "enable_autonomy": autonomy}
    with TestClient(build_agent_api(store, **config)) as client:
        response = client.get("/build-handoff", params={"connection_id": source_id})
        assert response.status_code == 200, response.text
        _assert_exact(response.json(), source_id, snapshot)

    async def run():
        server = build_quick_mcp_server(store, **config)
        tools = {tool.name: tool for tool in await server.list_tools()}
        annotations = tools["get_build_handoff"].annotations.model_dump(by_alias=True, exclude_none=True)
        assert annotations["readOnlyHint"] is True and annotations["destructiveHint"] is False
        result = await server.call_tool("get_build_handoff", {"connection_id": source_id})
        assert not result.is_error
        _assert_exact(result.structured_content, source_id, snapshot)

    asyncio.run(run())
    assert _writes(store) == before


def test_authorized_source_pages_and_unknown_denied_sources_preserve_privacy(registry):
    store, source_id, _snapshot = registry
    second_id, _ = _source(store, "Second")
    hidden_id, _ = _source(store, "Private secret title")
    for identifier in (source_id, second_id):
        store.grant_source_access("demo", identifier, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    reader = Principal("reader", frozenset())
    with principal_scope(reader), TestClient(build_agent_api(store)) as client:
        first = client.get("/build-handoff", params={"limit": 1}).json()
        second = client.get("/build-handoff", params=first["next_page"]["http_query"]).json()
        assert first["total_authorized_sources"] == second["total_authorized_sources"] == 2
        assert second["next_page"] is None
        assert {first["sources"][0]["connection_id"], second["sources"][0]["connection_id"]} == {source_id, second_id}
        assert "Private secret title" not in json.dumps(first) + json.dumps(second)
        denied = client.get("/build-handoff", params={"connection_id": hidden_id})
        unknown = client.get("/build-handoff", params={"connection_id": "missing"})
        assert denied.status_code == unknown.status_code == 404
        assert denied.json() == unknown.json()

    async def run():
        with principal_scope(reader):
            server = build_quick_mcp_server(store)
            first = (await server.call_tool("get_build_handoff", {"limit": 1})).structured_content
            second = (await server.call_tool("get_build_handoff", first["next_page"]["arguments"])).structured_content
            assert second["next_cursor"] is None
            with pytest.raises(ToolError, match="Connection was not found"):
                await server.call_tool("get_build_handoff", {"connection_id": hidden_id})

    asyncio.run(run())


def test_complete_response_budget_refuses_http_and_mcp_instead_of_clipping(registry, monkeypatch):
    store, source_id, _snapshot = registry
    monkeypatch.setattr(mcp_module, "MAX_CONTRACT_RESPONSE_BYTES", 100)
    with TestClient(build_agent_api(store)) as client:
        response = client.get("/build-handoff", params={"connection_id": source_id})
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "contract_response_too_large"
        assert "sources" not in response.json()

    async def run():
        with pytest.raises(ToolError, match="contract_response_too_large"):
            await build_quick_mcp_server(store).call_tool("get_build_handoff", {"connection_id": source_id})

    asyncio.run(run())


def test_changed_snapshot_between_projection_and_exact_evidence_refuses(registry, monkeypatch):
    store, source_id, _snapshot = registry
    original = handoff_module.maintenance_work

    def refresh_after_projection(*args, **kwargs):
        result = original(*args, **kwargs)
        store.save_snapshot("demo", source_id, _payload("Replacement"), source_refresh=True)
        return result

    monkeypatch.setattr(handoff_module, "maintenance_work", refresh_after_projection)
    with TestClient(build_agent_api(store)) as client:
        response = client.get("/build-handoff", params={"connection_id": source_id})
        assert response.status_code == 409
        assert "Source snapshot changed" in response.json()["detail"]

    async def run():
        with pytest.raises(ToolError, match="Source snapshot changed"):
            await build_quick_mcp_server(store).call_tool("get_build_handoff", {"connection_id": source_id})

    asyncio.run(run())


def test_capped_work_counts_remain_lower_bounds_with_both_track_navigation(registry):
    store, _source_id, _snapshot = registry
    source_id, _ = _source(store, "Large", concepts=501)
    source = build_handoff(store, "demo", source_id)["sources"][0]
    meaning = source["interpretation"]["meaning"]
    assert meaning["count_state"] == "lower_bound" and meaning["count"] == 200
    assert meaning["scan_complete"] is False and meaning["sample_only"] is True
    assert meaning["navigation"]["mcp_tool"] == "next_work"
    assert source["interpretation"]["contract"]["navigation"]["arguments"]["track"] == "contract"


def test_queued_discovery_returns_harness_resume_without_dispatch_or_available_work(registry):
    store, source_id, _snapshot = registry
    job = RefreshQueue(store).enqueue("demo", source_id)
    before = _writes(store)
    with TestClient(build_agent_api(store, durable_jobs=True)) as client:
        result = client.get("/build-handoff", params={"connection_id": source_id}).json()
        source = result["sources"][0]
        assert source["build_state"] == "discovery_pending"
        assert "refresh_active" in source["blockers"]
        assert source["interpretation"]["meaning"]["status"] == "blocked"
        assert source["interpretation"]["contract"]["work"] == []
        assert result["harness"]["agent_dispatched"] is False
        assert result["harness"]["resume_owner"] == "customer_harness"
        status = client.get(f"/jobs/{job['id']}")
        assert status.status_code == 200
        assert status.json()["job"]["status"] == "queued"
    assert _writes(store) == before


def test_revoked_grant_after_exact_evidence_fetch_drops_complete_source(registry, monkeypatch):
    store, source_id, snapshot = registry
    store.grant_source_access("demo", source_id, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    original_projection = handoff_module.maintenance_work
    original_connection = store.get_connection

    def revoke_after_exact_fetch(tenant, identifier):
        connection = original_connection(tenant, identifier)
        with store.engine.begin() as conn:
            conn.execute(delete(store.source_grants))
        return connection

    def install_revocation_after_projection(*args, **kwargs):
        result = original_projection(*args, **kwargs)
        monkeypatch.setattr(store, "get_connection", revoke_after_exact_fetch)
        return result

    monkeypatch.setattr(handoff_module, "maintenance_work", install_revocation_after_projection)
    with principal_scope(Principal("reader", frozenset())):
        result = build_handoff(store, "demo")
    assert result["sources"] == [] and result["total_authorized_sources"] == 0
    assert result["status"] == "empty"
    assert snapshot["evidence"][0]["summary"] not in json.dumps(result)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 21}, {"cursor": ""}, {"connection_id": "x", "cursor": "x"}])
def test_invalid_http_build_scope_refuses(registry, params):
    store, _source_id, _snapshot = registry
    with TestClient(build_agent_api(store)) as client:
        assert client.get("/build-handoff", params=params).status_code == 422
