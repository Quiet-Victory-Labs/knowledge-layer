"""Concurrent demo graph/list requests retain complete rows on shared SQLite."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from knowledge_layer.platform import create_platform
from knowledge_layer.store import KnowledgeStore


def test_shared_memory_sqlite_keeps_concurrent_list_and_graph_results_consistent(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_AUTH_MODE", "demo")
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,15\n", encoding="utf-8")
    (tmp_path / "customers.csv").write_text("customer_id,name\nC-1,Customer\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        with TestClient(create_platform(store=store)) as client:
            created = client.post("/api/knowledge/connections", json={
                "name": "Concurrent source", "kind": "local_files", "config": {"path": "."},
            })
            assert created.status_code == 201
            source_id = created.json()["connection"]["id"]
            refreshed = client.post(f"/api/knowledge/connections/{source_id}/refresh")
            assert refreshed.status_code == 200
            expected_graph = client.get("/api/knowledge/workspace/graph").json()
            assert expected_graph["nodes"]
            expected_nodes = {node["id"] for node in expected_graph["nodes"]}
            expected_edges = {edge["id"] for edge in expected_graph["edges"]}

            def request(index):
                if index % 2:
                    result = client.get("/api/knowledge/connections", params={"limit": 20})
                    assert result.status_code == 200
                    page = result.json()
                    assert page["total"] == 1
                    assert [item["id"] for item in page["connections"]] == [source_id]
                    assert page["connections"][0]["asset_count"] == 2
                else:
                    result = client.get("/api/knowledge/workspace/graph")
                    assert result.status_code == 200
                    graph = result.json()
                    assert {node["id"] for node in graph["nodes"]} == expected_nodes
                    assert {edge["id"] for edge in graph["edges"]} == expected_edges

            with ThreadPoolExecutor(max_workers=8) as pool:
                # Consume each future so unexpected row/parser exceptions fail
                # the test, alongside successful-but-corrupted row assertions.
                list(pool.map(request, range(128)))
    finally:
        store.close()
