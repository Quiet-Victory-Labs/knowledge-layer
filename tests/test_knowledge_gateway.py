"""The publishable gateway includes only guarded knowledge routes."""

from fastapi.testclient import TestClient

from knowledge_layer.gateway import create_gateway
from knowledge_layer.store import KnowledgeStore


def test_gateway_excludes_legacy_api_and_guards_http_and_mcp():
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={})
    store.save_snapshot("demo", connection["id"], {
        "status": "discovery_complete",
        "discovery_complete": True,
        "concepts": [{
            "id": "concept-order", "name": "Order", "status": "candidate",
            "semantic_type": "business_entity", "evidence_ids": ["evidence-order"],
            "candidate_definitions": [{"value": 14.0, "unit": "day"}],
        }, {"id": "concept-id", "name": "Id", "semantic_type": "identifier", "status": "candidate", "evidence_ids": ["evidence-order"]}],
        "assets": [{"id": "asset-order", "name": "orders.csv"}],
        "evidence": [{"id": "evidence-order", "asset_id": "asset-order", "summary": "Order fields"}],
        "relationships": [], "metrics": [], "proposals": [],
    })
    gateway = create_gateway(store=store, agent_token="known-token")
    with TestClient(gateway) as client:
        assert client.get("/api/health").status_code == 404
        assert client.get("/mcp/").status_code == 404
        assert client.get("/agent/api/sources").status_code == 401
        assert client.get("/agent/mcp/").status_code == 401
        assert client.get(f"/agent/api/graph?connection_id={connection['id']}&focus_id=concept-order").status_code == 401
        response = client.get(
            "/agent/api/sources", headers={"Authorization": "Bearer known-token"}
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        graph = client.get(
            f"/agent/api/graph?connection_id={connection['id']}&focus_id=concept-order",
            headers={"Authorization": "Bearer known-token"},
        )
        assert graph.status_code == 200
        assert graph.json()["nodes"][0]["status"] == "candidate"
        context = client.get(
            "/agent/api/context",
            params={"term": "Order", "connection_id": connection["id"]},
            headers={"Authorization": "Bearer known-token"},
        )
        assert context.status_code == 200
        assert context.json()["matches"][0]["concept"]["candidate_definitions"] == [
            {"value": 14.0, "unit": "day"}
        ]
        packet_response = client.get("/agent/api/curation-work", headers={"Authorization": "Bearer known-token"})
        assert packet_response.status_code == 200
        assert packet_response.json()["total"] == 1
        packet = packet_response.json()["work"][0]
        assert packet["target_concept_id"] == "concept-order"
        assert packet["evidence_ids"] == ["evidence-order"]
        assert packet["snapshot_id"]
        assert client.get("/agent/api/curation-work").status_code == 401
        submitted = client.post("/agent/api/proposals", headers={"Authorization": "Bearer known-token"}, json={
            "connection_id": connection["id"], "target_concept_id": packet["target_concept_id"],
            "expected_snapshot_id": packet["snapshot_id"],
            "evidence_ids": packet["evidence_ids"],
            "rationale": "The cited source fields identify an order record.",
            "proposed_definition": "An order is a source record representing a customer request.",
        })
        assert submitted.status_code == 202
        assert submitted.json()["approval_required"] is True
        assert client.get("/agent/api/curation-work", headers={"Authorization": "Bearer known-token"}).json()["total"] == 0
        stale = client.post("/agent/api/proposals", headers={"Authorization": "Bearer known-token"}, json={
            "connection_id": connection["id"], "target_concept_id": packet["target_concept_id"],
            "expected_snapshot_id": "outdated-snapshot", "evidence_ids": packet["evidence_ids"],
            "rationale": "The cited source fields identify an order record.",
            "proposed_definition": "An order is a source record.",
        })
        assert stale.status_code == 409
    store.close()


def test_agent_can_connect_only_operator_enabled_sources(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,15\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    gateway = create_gateway(
        store=store, agent_token="known-token", enable_source_management=True,
        allowed_connection_kinds=frozenset({"local_files"}),
    )
    headers = {"Authorization": "Bearer known-token"}
    with TestClient(gateway) as client:
        assert client.get("/agent/api/connector-types").status_code == 401
        options = client.get("/agent/api/connector-types", headers=headers)
        assert [item["id"] for item in options.json()["connector_types"]] == ["local_files"]
        denied = client.post("/agent/api/sources", headers=headers, json={
            "name": "Unapproved", "kind": "sql_catalog", "config": {},
        })
        assert denied.status_code == 422
        created = client.post("/agent/api/sources", headers=headers, json={
            "name": "Customer orders", "kind": "local_files", "config": {"path": "."},
        })
        assert created.status_code == 201
        body = created.json()
        assert body["discovery_complete"] is True
        assert body["agent_ready"] is False
        assert body["counts"]["assets"] == 1
        connection_id = body["connection"]["id"]
        status = client.get(f"/agent/api/sources/{connection_id}/status", headers=headers).json()
        assert status["agent_ready"] is False
        assert status["snapshot_id"] == body["snapshot_id"]
        work = client.get("/agent/api/curation-work", headers=headers, params={"connection_id": connection_id}).json()
        assert work["total"] > 0
        refreshed = client.post(f"/agent/api/sources/{connection_id}/refresh", headers=headers)
        assert refreshed.status_code == 200
        assert refreshed.json()["snapshot_id"] == body["snapshot_id"]
    store.close()
