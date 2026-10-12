"""Ranked lexical candidates stay explicit across native agent interfaces."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.client import KnowledgeClient
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "policy.md").write_text(
        "# Forecast policy\n\nFrozen forecast requires separate review.\n", encoding="utf-8",
    )
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Synthetic policy", config={
        "path": ".", "retain_semantic_samples": True,
    })
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    yield store, source, snapshot
    store.close()


def _assert_partial(matches, source, snapshot):
    assert matches
    assert all(match["match_type"] == "partial" for match in matches)
    assert all(match["unmatched_terms"] == ["controls"] for match in matches)
    assert all(match["matched_terms"] == ["forecast", "frozen"] for match in matches)
    assert all(match["evidence_ref"]["connection_id"] == source["id"] for match in matches)
    assert all(match["evidence_ref"]["snapshot_id"] == snapshot["id"] for match in matches)


def test_sdk_documents_require_explicit_ranked_mode(registry):
    store, source, snapshot = registry
    client = KnowledgeClient(store=store, tenant_id="demo")
    assert client.search_evidence("frozen forecast controls") == []
    _assert_partial(client.search_evidence("frozen forecast controls", match_mode="ranked"), source, snapshot)
    assert client.search_evidence("Please tell me about frozen forecast")
    with pytest.raises(ValueError, match="omit connection_id"):
        client.search_evidence("frozen forecast controls", connection_id=source["id"], match_mode="ranked")
    with pytest.raises(ValueError, match="match_mode"):
        client.search_evidence("frozen forecast", match_mode="semantic")


@pytest.mark.parametrize("builder,tool_name", [
    (build_quick_mcp_server, "search_evidence"),
    (build_knowledge_mcp_server, "search_document_evidence"),
])
def test_mcp_ranked_mode_schema_and_citation_parity(registry, builder, tool_name):
    store, source, snapshot = registry
    server = builder(store)

    async def run():
        tools = {tool.name: tool for tool in await server.list_tools()}
        mode = tools[tool_name].input_schema["properties"]["match_mode"]
        assert mode["default"] == "all"
        assert set(mode["enum"]) == {"all", "ranked"}
        default = await server.call_tool(tool_name, {"query": "frozen forecast controls"})
        assert not default.is_error
        assert default.structured_content["matches"] == []
        ranked = await server.call_tool(tool_name, {
            "query": "frozen forecast controls", "match_mode": "ranked",
        })
        assert not ranked.is_error
        assert ranked.structured_content["search_mode"] == "lexical_ranked_partial_terms"
        _assert_partial(ranked.structured_content["matches"], source, snapshot)
        with pytest.raises(ToolError, match="match_mode"):
            await server.call_tool(tool_name, {"query": "frozen forecast", "match_mode": "semantic"})

    asyncio.run(run())


def test_http_ranked_mode_is_bounded_and_explicit(registry):
    store, source, snapshot = registry
    with TestClient(build_agent_api(store)) as client:
        default = client.get("/document-evidence/search", params={"q": "frozen forecast controls"})
        assert default.status_code == 200
        assert default.json()["matches"] == []
        ranked = client.get("/document-evidence/search", params={
            "q": "frozen forecast controls", "match_mode": "ranked", "limit": 1,
        })
        assert ranked.status_code == 200
        assert len(ranked.json()["matches"]) == 1
        _assert_partial(ranked.json()["matches"], source, snapshot)
        assert client.get("/document-evidence/search", params={
            "q": "frozen forecast", "match_mode": "semantic",
        }).status_code == 422


def test_compact_mcp_rejects_ranked_metadata_scope(registry):
    store, source, _ = registry
    server = build_quick_mcp_server(store)

    async def run():
        with pytest.raises(ToolError, match="requires scope documents"):
            await server.call_tool("search_evidence", {
                "query": "frozen forecast", "connection_id": source["id"], "match_mode": "ranked",
            })

    asyncio.run(run())


def test_term_diagnostics_are_complete_across_all_interfaces(registry, tmp_path):
    store, source, _ = registry
    query = " ".join(f"t{index}" for index in range(30))
    assert len(query) <= 120
    (tmp_path / "policy.md").write_text("# Synthetic terms\n\n" + query + "\n", encoding="utf-8")
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    sdk = KnowledgeClient(store=store, tenant_id="demo")
    expected = sdk.search_evidence(query, match_mode="ranked")
    assert len(expected) == 1
    assert len(expected[0]["matched_terms"]) == 30
    assert expected[0]["evidence_ref"]["snapshot_id"] == snapshot["id"]
    with TestClient(build_agent_api(store)) as client:
        response = client.get("/document-evidence/search", params={"q": query, "match_mode": "ranked"})
        assert response.status_code == 200
        assert response.json()["matches"] == expected
        assert len(response.json()["query_terms"]) == 30

    async def run():
        for builder, name in ((build_quick_mcp_server, "search_evidence"),
                              (build_knowledge_mcp_server, "search_document_evidence")):
            result = await builder(store).call_tool(name, {"query": query, "match_mode": "ranked"})
            assert not result.is_error
            assert result.structured_content["matches"] == expected
            assert len(result.structured_content["query_terms"]) == 30

    asyncio.run(run())
