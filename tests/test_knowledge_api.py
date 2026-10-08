from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_layer.api import create_knowledge_router
from knowledge_layer.store import KnowledgeStore


def _snapshot() -> dict:
    return {
        "snapshot_id": "fixture-1",
        "kind": "local_files",
        "status": "discovery_complete",
        "discovery_complete": True,
        "meaning_certified": False,
        "agent_ready": False,
        "curation_status": "candidate_review_required",
        "source": {"id": "source-1", "name": "Fixture", "kind": "local_files", "trust_state": "unverified"},
        "capabilities": {"metadata": True, "query_execution": False},
        "assets": [{"id": "asset-1", "name": "orders.csv", "asset_type": "table", "schema": [], "profile": {"row_count": 3}, "trust_state": "observed"}],
        "concepts": [{"id": "concept-revenue", "name": "Revenue", "definition": "Source definition", "status": "candidate", "trust_state": "inferred", "evidence_ids": ["evidence-1"]}],
        "relationships": [],
        "proposals": [],
        "metrics": [{"id": "metric-1", "name": "Revenue", "unit": "USD", "grain": "order", "status": "candidate", "certified": False}],
        "evidence": [{"id": "evidence-1", "asset_id": "asset-1", "summary": "Profile suggests this field contains order totals."}],
        "drift": {"added_assets": [], "removed_assets": [], "changed_assets": [], "previous_snapshot_id": None},
        "errors": [],
    }


def _client(tmp_path) -> tuple[TestClient, KnowledgeStore]:
    fixture_root = tmp_path / "fixtures"
    (fixture_root / "juniper").mkdir(parents=True)
    (fixture_root / "northstar").mkdir(parents=True)
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'api.db'}", local_files_root=fixture_root)
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store))
    return TestClient(app), store


def test_connection_routes_use_fixed_demo_tenant_and_return_compact_contract(tmp_path):
    client, store = _client(tmp_path)
    with client:
        connector_types = client.get("/api/knowledge/connector-types")
        assert connector_types.status_code == 200
        types = connector_types.json()["connector_types"]
        assert {"local_files", "postgres", "sql_catalog"} <= {item["id"] for item in types}
        assert {item["id"] for item in types} <= {"local_files", "iceberg", "postgres", "sql_catalog", "object_store", "google_drive", "external_adapter"}
        local = next(item for item in types if item["id"] == "local_files")
        path_field = next(field for field in local["config_fields"] if field["name"] == "path")
        assert path_field["options"] == [
            {"label": "Juniper Market", "value": "juniper"},
            {"label": "Northstar Field Services", "value": "northstar"},
        ]

        created = client.post("/api/knowledge/connections", json={"name": "Orders", "kind": "local_files", "config": {"path": "."}})
        assert created.status_code == 201
        connection = created.json()["connection"]
        assert connection["tenant_id"] == "demo"
        assert "root" not in connection["config"]

        rows = client.get("/api/knowledge/connections").json()["connections"]
        assert rows[0]["id"] == connection["id"]
        assert "config" not in rows[0]
        detail = client.get(f"/api/knowledge/connections/{connection['id']}").json()
        assert detail["connection"]["id"] == connection["id"]
        assert detail["snapshot"] is None
        updated = client.patch(
            f"/api/knowledge/connections/{connection['id']}",
            json={"config": {"path": "juniper", "retain_semantic_samples": True}},
        )
        assert updated.status_code == 200
        assert updated.json()["connection"]["config"]["retain_semantic_samples"] is True
        assert updated.json()["connection"]["config"]["path"] == "juniper"
        assert client.patch(
            f"/api/knowledge/connections/{connection['id']}",
            json={"config": {"path": "juniper", "retain_semantic_samples": "yes"}},
        ).status_code == 422

        private = store.create_connection("another-tenant", kind="local_files", name="Private", config={})
        hidden = client.get(f"/api/knowledge/connections/{private['id']}")
        assert hidden.status_code == 404
        assert client.patch(f"/api/knowledge/connections/{private['id']}", json={"config": {}}).status_code == 404
        cannot_select = client.get("/api/knowledge/connections?tenant_id=another-tenant")
        assert [row["id"] for row in cannot_select.json()["connections"]] == [connection["id"]]

        body_tenant = client.post("/api/knowledge/connections", json={"tenant_id": "another-tenant", "name": "Private", "kind": "local_files", "config": {}})
        assert body_tenant.status_code == 422
        leaked_secret = client.post("/api/knowledge/connections", json={"name": "Unsafe", "kind": "postgres", "config": {"secret_ref": "DATABASE_URL"}})
        assert leaked_secret.status_code == 422
    store.close()


def test_refresh_concept_and_review_routes_preserve_status_evidence_and_history(tmp_path):
    client, store = _client(tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={})
    store.save_snapshot("demo", connection["id"], _snapshot())
    with client:
        detail = client.get(f"/api/knowledge/connections/{connection['id']}").json()
        assert detail["snapshot"]["status"] == "discovery_complete"
        assert detail["snapshot"]["agent_ready"] is False
        assert detail["snapshot"]["metrics"][0]["certified"] is False

        context = client.get("/api/knowledge/concepts/concept-revenue").json()
        assert context["concept"]["definition"] == "Source definition"
        assert context["snapshot_version"] == 1
        assert context["evidence_ids"] == ["evidence-1"]
        graph = client.get(f"/api/knowledge/connections/{connection['id']}/graph?focus_id=concept-revenue&depth=2")
        assert graph.status_code == 200
        assert graph.json()["version"] == 1
        assert {node["id"] for node in graph.json()["nodes"]} == {"concept-revenue", "evidence-1", "asset-1"}
        assert client.get(f"/api/knowledge/connections/{connection['id']}/graph?limit=101").status_code == 422

        created = client.post(
            f"/api/knowledge/connections/{connection['id']}/proposals",
            json={
                "target_concept_id": "concept-revenue",
                "proposed_definition": "Revenue is the net order amount after approved refunds.",
                "evidence_ids": ["evidence-1"],
                "rationale": "This corrects the source definition using reviewed evidence.",
            },
        )
        assert created.status_code == 201
        proposal = created.json()["proposal"]
        assert proposal["status"] == "pending_review"
        assert proposal["certified"] is False

        decision = client.post(
            f"/api/knowledge/connections/{connection['id']}/proposals/{proposal['id']}/decision",
            json={"decision": "approve", "rationale": "Evidence verified by steward."},
        )
        assert decision.status_code == 200
        assert decision.json()["proposal"]["status"] == "approved"
        current = client.get("/api/knowledge/concepts/concept-revenue").json()
        assert current["concept"]["definition"].startswith("Revenue is the net order amount")
        assert current["snapshot_version"] == 2
        prior = store.get_snapshot("demo", connection["id"], version=1)
        assert prior["concepts"][0]["definition"] == "Source definition"

        conflict = client.post(
            f"/api/knowledge/connections/{connection['id']}/proposals/{proposal['id']}/decision",
            json={"decision": "reject"},
        )
        assert conflict.status_code == 409
    store.close()
