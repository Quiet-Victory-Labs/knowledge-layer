"""Advertised MCP effects preserve conservative handling of state-changing tools."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_layer.mcp import READ_ONLY_ANNOTATIONS, build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server

PURE_READ_HINTS = {"readOnlyHint": True, "destructiveHint": False,
                   "openWorldHint": False, "idempotentHint": True}
SHARED_READERS = {"get_refresh_schedule", "get_refresh_job", "get_ontology_export",
                  "resolve_guidance", "get_guidance", "export_guidance_bundle",
                  "get_capabilities", "plan_metric_query", "plan_relationship",
                  "get_task_context", "revalidate_task_context", "get_maintenance_work", "get_inventory"}
READERS = {
    "quick": SHARED_READERS | {"get_workspace", "next_work", "search_evidence",
                               "resolve_context", "get_contract"},
    "advanced": SHARED_READERS | {"list_knowledge_sources", "list_connectable_source_types",
        "get_curation_status", "list_curation_work", "list_contract_work", "search_source_evidence",
        "search_document_evidence", "get_semantic_contract", "get_ossie_preview",
        "resolve_business_term", "get_concept_context", "get_relationship_neighborhood",
        "get_federated_evidence_graph"},
}
SHARED_MUTATIONS = {"set_refresh_schedule", "cancel_refresh_job", "draft_guidance",
                    "stage_inventory", "apply_autonomy_policy"}
MUTATIONS = {
    "quick": SHARED_MUTATIONS | {"connect_source", "refresh_source", "propose_meaning", "propose_contract"},
    "advanced": SHARED_MUTATIONS | {"connect_knowledge_source", "refresh_knowledge_source",
                                   "propose_semantic_change", "propose_contract_assertion"},
}


@pytest.fixture(scope="module")
def advertised_tools():
    # Listing invokes no tool body, registry, source connector, or model.
    builders = {"quick": build_quick_mcp_server, "advanced": build_knowledge_mcp_server}
    servers = {name: builder(object(), enable_source_management=True, durable_jobs=True,
                             enable_autonomy=True) for name, builder in builders.items()}

    @asynccontextmanager
    async def lifespan(_app):
        async with servers["quick"].session_manager.run(), servers["advanced"].session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    for name, server in servers.items():
        app.mount("/" + name, server.streamable_http_app(
            streamable_http_path="/", json_response=True, stateless_http=True))
    result = {}
    with TestClient(app, base_url="http://127.0.0.1:4173") as client:
        for name in servers:
            response = client.post("/" + name + "/", headers={
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert response.status_code == 200
            result[name] = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    return result


def test_shared_hint_object_uses_installed_sdk_wire_aliases():
    assert READ_ONLY_ANNOTATIONS.model_dump(by_alias=True, exclude_none=True) == PURE_READ_HINTS


@pytest.mark.parametrize("surface", ["quick", "advanced"])
def test_real_sdk_lists_literal_readonly_reader_effects(advertised_tools, surface):
    tools = advertised_tools[surface]
    assert READERS[surface] <= tools.keys()
    for name in READERS[surface]:
        assert tools[name]["annotations"] == PURE_READ_HINTS, name
        assert tools[name]["annotations"]["readOnlyHint"] is True


@pytest.mark.parametrize("surface", ["quick", "advanced"])
def test_mutations_never_advertise_readonly_execution(advertised_tools, surface):
    tools = advertised_tools[surface]
    assert MUTATIONS[surface] <= tools.keys()
    for name in MUTATIONS[surface]:
        assert (tools[name].get("annotations") or {}).get("readOnlyHint") is not True, name


def test_optional_cold_embedding_search_stays_conservative(advertised_tools):
    # Search may initialize FastEmbed weights in the background and write a local cache.
    tool = advertised_tools["advanced"]["search_knowledge"]
    assert (tool.get("annotations") or {}).get("readOnlyHint") is not True
