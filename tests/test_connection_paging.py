"""Authorized source lists remain reachable beyond their first bounded page."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.platform import create_platform
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore, KnowledgeValidationError


def test_connection_pages_are_stable_bounded_and_grant_filtered(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    ids = [store.create_connection("demo", kind="local_files", name=f"Source {index:02d}", config={})["id"]
           for index in range(53)]
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).values(created_at="2026-10-02T00:00:00.000000Z"))
    for source_id in ids[::2]:
        store.grant_source_access("demo", source_id, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    reader = Principal("reader", frozenset())
    with principal_scope(reader):
        cursor = None
        seen = []
        while True:
            page = store.page_connections("demo", limit=7, cursor=cursor)
            seen.extend(item["id"] for item in page["connections"])
            assert len(page["connections"]) <= 7
            assert page["total"] == 27
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 27
        assert set(seen) == set(ids[::2])
        with pytest.raises(KnowledgeValidationError):
            store.page_connections("demo", limit=7, cursor="not-a-valid-cursor")
        with pytest.raises(KnowledgeValidationError):
            store.page_connections("another", limit=7, cursor=store.page_connections("demo", limit=7)["next_cursor"])
        first = store.page_connections("demo", limit=7)
    store.access_enforced = False
    added = store.create_connection("demo", kind="local_files", name="Later source", config={})["id"]
    store.grant_source_access("demo", added, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    with principal_scope(reader):
        assert store.page_connections("demo", limit=7)["connections"][0]["id"] == added
        after_first = store.page_connections("demo", limit=100, cursor=first["next_cursor"])
        assert after_first["total"] == 28
        assert added not in {item["id"] for item in after_first["connections"]}
        assert len(after_first["connections"]) == 20
    store.close()


def test_browser_http_agent_and_mcp_can_page_every_source(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    expected = {store.create_connection("demo", kind="local_files", name=f"Source {index:02d}", config={})["id"]
                for index in range(52)}
    app = create_platform(store=store, agent_token="known-token")
    with TestClient(app) as client:
        for path, headers in (("/api/knowledge/connections", {}),
                              ("/agent/api/sources", {"Authorization": "Bearer known-token"})):
            cursor = None
            seen = []
            while True:
                params = {"limit": 20}
                if cursor:
                    params["cursor"] = cursor
                response = client.get(path, params=params, headers=headers)
                assert response.status_code == 200
                page = response.json()
                assert page["total"] == 52
                seen.extend(item["id"] for item in page["connections"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert set(seen) == expected
            assert len(seen) == 52
        assert client.get("/api/knowledge/connections?cursor=invalid").status_code == 422

    async def check_mcp():
        for server, tool_name, key in (
            (build_quick_mcp_server(store), "get_workspace", "sources"),
            (build_knowledge_mcp_server(store), "list_knowledge_sources", "connections"),
        ):
            cursor = None
            seen = []
            while True:
                arguments = {"limit": 20} | ({"cursor": cursor} if cursor else {})
                page = (await server.call_tool(tool_name, arguments)).structured_content
                seen.extend(item["id"] for item in page[key])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            assert set(seen) == expected
            assert len(seen) == 52

    asyncio.run(check_mcp())
    store.close()
