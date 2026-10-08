"""Company instructions cross HTTP/MCP without giving agents publication powers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore


def _payload():
    path = Path(__file__).resolve().parents[1] / "examples/company-guidance-synthetic.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("surface", ["browser", "agent"])
def test_http_company_guidance_draft_scope_exact_text_and_publication_boundary(surface):
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        from knowledge_layer.guidance import GuidanceRegistry

        if surface == "browser":
            app = FastAPI()
            app.include_router(create_knowledge_router(store=store))
            base = "/api/knowledge/guidance"
        else:
            app = build_agent_api(store)
            base = "/guidance"
        payload = _payload()
        payload["skills"][0]["instructions"] += "\nExact Unicode evidence: café — " + "detail " * 300
        with TestClient(app) as client:
            assert client.get(f"{base}/unknown").status_code == 404
            assert client.get(f"{base}/resolve").json()["status"] == "empty"
            assert client.post(f"{base}/dfs/draft", json={"payload": payload, "expected_revision": True}).status_code == 422
            assert client.post(f"{base}/dfs/draft", json={"payload": payload, "unexpected": 1}).status_code == 422
            drafted = client.post(f"{base}/dfs/draft", json={"payload": payload})
            assert drafted.status_code == 201
            row = drafted.json()
            assert row["revision"] == 1
            assert client.get(f"{base}/resolve?business_unit=synthetic-analytics&task=sales-analysis").json()["status"] == "empty"
            pub = {"revision": row["revision"], "expected_digest": row["digest"],
                   "rationale": "The operator reviewed this bounded procedure template."}
            if surface == "browser":
                assert client.post(f"{base}/dfs/publish", json=pub).status_code == 200
            else:
                assert client.post(f"{base}/dfs/publish", json=pub).status_code == 404
                assert client.post(f"{base}/dfs/withdraw", json={"revision": 1, "reason": pub["rationale"]}).status_code == 404
                GuidanceRegistry(store).publish("demo", "dfs", **pub)
            assert client.get(f"{base}/resolve").json()["status"] == "empty"
            query = "business_unit=synthetic-analytics&task=sales-analysis"
            current = client.get(f"{base}/resolve?{query}").json()
            assert current["status"] == "ready"
            assert current["bundles"][0]["payload"]["skills"][0]["instructions"] == payload["skills"][0]["instructions"]
            export_response = client.get(f"{base}/export?{query}")
            assert export_response.status_code == 200, export_response.json()
            exported = export_response.json()
            assert payload["skills"][0]["instructions"] in "\n".join(exported["files"].values())
            stale = client.post(f"{base}/dfs/draft", json={"payload": payload, "expected_revision": 0})
            assert stale.status_code == 409
    finally:
        store.close()


def test_http_guidance_role_checks_and_separate_steward_publication():
    store = KnowledgeStore(database_url="sqlite:///:memory:", access_enforced=True)
    store.source_admin_group = "stewards"
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store))
    base = "/api/knowledge/guidance/dfs"
    reader = Principal("reader", frozenset())
    manager = Principal("manager", frozenset(), frozenset({"knowledge.sources.manage"}))
    steward = Principal("steward", frozenset({"stewards"}))
    try:
        with TestClient(app) as client:
            with principal_scope(reader):
                assert client.post(f"{base}/draft", json={"payload": _payload()}).status_code == 403
            with principal_scope(manager):
                row = client.post(f"{base}/draft", json={"payload": _payload()}).json()
                pub = {"revision": row["revision"], "expected_digest": row["digest"],
                       "rationale": "The company steward reviewed this procedure template."}
                assert client.post(f"{base}/publish", json=pub).status_code == 403
            with principal_scope(steward):
                assert client.post(f"{base}/publish", json=pub).status_code == 200
            with principal_scope(reader):
                current = client.get("/api/knowledge/guidance/resolve?business_unit=synthetic-analytics&task=sales-analysis")
                assert current.status_code == 200 and current.json()["status"] == "ready"
                assert client.post(f"{base}/withdraw", json={"revision": 1, "reason": pub["rationale"]}).status_code == 403
    finally:
        store.close()


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server])
def test_mcp_company_guidance_roundtrip_and_no_publication_tools(builder):
    store = KnowledgeStore(database_url="sqlite:///:memory:")

    async def run():
        from knowledge_layer.guidance import GuidanceRegistry

        server = builder(store)
        names = {tool.name for tool in await server.list_tools()}
        assert {"resolve_guidance", "get_guidance", "draft_guidance", "export_guidance_bundle"} <= names
        assert not {"publish_guidance", "revoke_guidance"}.intersection(names)
        payload = _payload()
        with pytest.raises(ToolError, match="Guidance was not found"):
            await server.call_tool("get_guidance", {"slug": "unknown"})
        with pytest.raises(ToolError, match="valid integer"):
            await server.call_tool("draft_guidance", {"slug": "dfs", "payload": payload, "expected_revision": True})
        row = (await server.call_tool("draft_guidance", {"slug": "dfs", "payload": payload})).structured_content
        assert row["revision"] == 1
        assert (await server.call_tool("get_guidance", {"slug": "dfs"})).structured_content["digest"] == row["digest"]
        args = {"business_unit": "synthetic-analytics", "task": "sales-analysis"}
        assert (await server.call_tool("resolve_guidance", args)).structured_content["status"] == "empty"
        GuidanceRegistry(store).publish("demo", "dfs", revision=1, expected_digest=row["digest"],
                                        rationale="The operator approved this reviewable procedure template.")
        resolved = (await server.call_tool("resolve_guidance", args)).structured_content
        assert resolved["status"] == "ready"
        assert resolved["bundles"][0]["payload"]["skills"][0]["instructions"] == payload["skills"][0]["instructions"]
        files = (await server.call_tool("export_guidance_bundle", args)).structured_content["files"]
        assert payload["skills"][0]["instructions"] in "\n".join(files.values())
        GuidanceRegistry(store).revoke("demo", "dfs", revision=1,
                                       reason="The operator withdrew this procedure template.")
        assert (await server.call_tool("resolve_guidance", args)).structured_content["status"] == "blocked"
        with pytest.raises(ToolError, match="Only ready guidance"):
            await server.call_tool("export_guidance_bundle", args)

    try:
        asyncio.run(run())
    finally:
        store.close()
