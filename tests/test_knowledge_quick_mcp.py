"""Default MCP surface supports the customer agent's complete first pass."""

from __future__ import annotations

import asyncio
import json

from rdflib import Graph

from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore


def test_quick_mcp_connects_cures_and_leaves_meaning_pending(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,15\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    server = build_quick_mcp_server(store, enable_source_management=True)

    async def run():
        names = {tool.name for tool in await server.list_tools()}
        assert names == {
            "get_workspace", "get_build_handoff", "build_knowledge", "connect_source", "refresh_source", "next_work",
            "search_evidence", "propose_meaning", "propose_contract", "resolve_context", "get_contract",
            "get_ontology_export", "get_refresh_schedule", "set_refresh_schedule",
            "resolve_guidance", "get_guidance", "draft_guidance", "export_guidance_bundle",
            "plan_metric_query", "plan_relationship", "get_capabilities",
            "get_task_context", "revalidate_task_context",
            "get_maintenance_work", "get_inventory", "stage_inventory", "curate_inventory",
            "get_workspace_graph", "get_workspace_ontology",
        }
        workspace = (await server.call_tool("get_workspace", {})).structured_content
        assert workspace["sources"] == []
        created = (await server.call_tool("connect_source", {
            "name": "Customer orders", "kind": "local_files", "config": {"path": "."},
        })).structured_content
        connection_id = created["connection"]["id"]
        assert created["discovery_complete"] is True
        compact_workspace = (await server.call_tool("get_workspace", {})).structured_content
        assert compact_workspace["sources"][0]["id"] == connection_id
        assert len(json.dumps(compact_workspace).encode("utf-8")) < 2_000
        again = (await server.call_tool("connect_source", {
            "name": "Customer orders", "kind": "local_files", "config": {"path": "."},
        })).structured_content
        assert again["connection"]["id"] == connection_id
        assert again["reused_existing"] is True
        assert again["snapshot_version"] == created["snapshot_version"]
        assert len(store.list_connections("demo")) == 1
        work = (await server.call_tool("next_work", {
            "connection_id": connection_id, "track": "meaning", "limit": 3,
        })).structured_content
        assert work["work"] and work["work"][0]["submission_tool"] == "propose_meaning"
        packet = next(item for item in work["work"] if item["kind"] == "define_concept")
        proposal = (await server.call_tool("propose_meaning", {
            "connection_id": connection_id,
            "target_concept_id": packet["target_concept_id"],
            "expected_snapshot_id": packet["snapshot_id"],
            "evidence_ids": packet["evidence_ids"],
            "rationale": "The field name and source profile indicate an order identifier.",
            "proposed_definition": "A source order record identifier.",
        })).structured_content
        assert proposal["approval_required"] is True
        assert proposal["proposal"]["status"] == "pending_review"
        contract_work = (await server.call_tool("next_work", {
            "connection_id": connection_id, "track": "contract", "limit": 2,
        })).structured_content
        assert contract_work["work"][0]["submission_tool"] == "propose_contract"
        asset_id = contract_work["work"][0]["asset_id"]
        contract = (await server.call_tool("get_contract", {
            "connection_id": connection_id, "asset_id": asset_id,
        })).structured_content
        assert contract["dataset"]["asset_id"] == asset_id
        assert contract["agent_ready"] is False
        rdf = (await server.call_tool("get_ontology_export", {
            "connection_id": connection_id,
        })).structured_content
        assert rdf["media_type"] == "text/turtle"
        assert rdf["connection_id"] == connection_id
        assert rdf["snapshot_id"] == contract["snapshot_id"]
        assert rdf["snapshot_version"] == contract["snapshot_version"]
        assert len(Graph().parse(data=rdf["turtle"], format="turtle")) > 0

    asyncio.run(run())
    store.close()


def test_unscoped_evidence_search_finds_authorized_documents(tmp_path):
    (tmp_path / "experiment.md").write_text(
        "# Experiment gates\nAn independent grader reviews the run.\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Experiment policy",
                                     config={"path": ".", "retain_semantic_samples": True})
    store.refresh_connection("demo", source["id"])
    server = build_quick_mcp_server(store)

    async def run():
        result = await server.call_tool("search_evidence", {"query": "independent grader", "limit": 3})
        assert not result.is_error
        assert result.structured_content["matches"]
        assert result.structured_content["matches"][0]["connection_id"] == source["id"]

    asyncio.run(run())
    store.close()
