"""HTTP and both MCP surfaces expose read-only discovery selection to readers."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.inventory_transport import inventory_operation
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeValidationError

spec = importlib.util.spec_from_file_location(
    "selection_transport_fixtures", Path(__file__).with_name("test_inventory_selection.py"))
assert spec and spec.loader
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
registry = fixtures.registry
base = fixtures.base


@pytest.fixture
def committed(registry):
    store, source_id, inventory = registry
    row = base.entity(refs=("exact-proof",))
    row["payload"]["definition"] = "Exact complete Unicode definition: 完整.\nDo not infer approval."
    with principal_scope(base.OWNER):
        run = fixtures._complete(inventory, source_id, entities=[row],
                                 evidence=[base.evidence("exact-proof")])
    return store, source_id, inventory, run, row


def _body(run, **changes):
    return {"payload": {"expected_manifest_digest": run["manifest_digest"],
                        "entity_ids": ["entity.1"], **changes}}


def test_reader_http_discovers_scope_and_selects_with_management_routes_absent(committed):
    store, source_id, _inventory, run, row = committed
    before = fixtures._state(store)
    with principal_scope(base.READER), TestClient(build_agent_api(store)) as client:
        scopes = client.get(f"/sources/{source_id}/inventory/scopes")
        assert scopes.status_code == 200, scopes.text
        assert [item["id"] for item in scopes.json()["items"]] == [run["id"]]
        result = client.post(f"/sources/{source_id}/inventory/{run['id']}/select", json=_body(run))
        assert result.status_code == 200, result.text
        packet = result.json()
        assert packet["entities"][0]["payload"] == row["payload"]
        assert packet["evidence"][0]["id"] == "exact-proof"
        assert packet["meaning_certified"] is False and packet["agent_ready"] is False
        assert packet["source_reads_executed"] is False and packet["text_complete"] is True
        assert client.post(f"/sources/{source_id}/inventory/begin", json={"payload": {}}).status_code == 405
    assert fixtures._state(store) == before


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server],
                         ids=["compact", "advanced"])
def test_both_mcp_surfaces_allow_exact_inventory_reads_without_mutation_tools(committed, builder):
    store, source_id, _inventory, run, row = committed
    before = fixtures._state(store)
    async def consume():
        server = builder(store)
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert "stage_inventory" not in tools
        annotations = tools["get_inventory"].annotations.model_dump(by_alias=True, exclude_none=True)
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        with principal_scope(base.READER):
            scopes = await server.call_tool("get_inventory", {
                "connection_id": source_id, "operation": "scopes", "payload": {"limit": 10}})
            assert not scopes.is_error
            assert scopes.structured_content["items"][0]["id"] == run["id"]
            result = await server.call_tool("get_inventory", {
                "connection_id": source_id, "operation": "select",
                "payload": {"inventory_id": run["id"], **_body(run)["payload"]}})
            assert not result.is_error
            assert result.structured_content["entities"][0]["payload"] == row["payload"]
            assert result.structured_content["meaning_certified"] is False
    asyncio.run(consume())
    assert fixtures._state(store) == before


@pytest.mark.parametrize("extra", [
    {"tenant_id": "other"}, {"claim": {"attempt": 1}}, {"principal": "owner"},
    {"agent_ready": True}, {"meaning_certified": True},
])
def test_http_and_dispatch_reject_injected_authority(committed, extra):
    store, source_id, _inventory, run, _row = committed
    with principal_scope(base.READER), TestClient(build_agent_api(store)) as client:
        response = client.post(f"/sources/{source_id}/inventory/{run['id']}/select",
                               json=_body(run, **extra))
        assert response.status_code == 422, response.text
        with pytest.raises(KnowledgeValidationError):
            inventory_operation(store, source_id, "select", {
                "inventory_id": run["id"], **_body(run, **extra)["payload"]})


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server],
                         ids=["compact", "advanced"])
def test_denied_readers_receive_neither_scope_titles_nor_selected_evidence(committed, builder):
    store, source_id, _inventory, run, row = committed
    stranger = Principal("ungranted-user", frozenset())
    with principal_scope(stranger), TestClient(build_agent_api(store)) as client:
        for path, method in [(f"/sources/{source_id}/inventory/scopes", client.get),
                             (f"/sources/{source_id}/inventory/{run['id']}/select", client.post)]:
            response = method(path, **({"json": _body(run)} if method == client.post else {}))
            assert response.status_code == 404, response.text
            assert row["payload"]["definition"] not in response.text
            assert "exact-proof" not in response.text
            assert run["scope_digest"] not in response.text
    async def refuse():
        from mcp.server.mcpserver.exceptions import ToolError

        server = builder(store)
        with principal_scope(stranger):
            for operation, payload in [("scopes", {}), ("select", {
                "inventory_id": run["id"], **_body(run)["payload"]})]:
                with pytest.raises(ToolError, match="not found") as error:
                    await server.call_tool("get_inventory", {
                        "connection_id": source_id, "operation": operation, "payload": payload})
                assert "exact-proof" not in str(error.value)
    asyncio.run(refuse())


def test_http_distinguishes_unknown_entity_from_changed_manifest(committed):
    store, source_id, _inventory, run, _row = committed
    path = f"/sources/{source_id}/inventory/{run['id']}/select"
    with principal_scope(base.READER), TestClient(build_agent_api(store)) as client:
        unknown = client.post(path, json=_body(run, entity_ids=["missing"]))
        stale = client.post(path, json=_body(run, expected_manifest_digest="0" * 64))
        assert unknown.status_code == 404
        assert stale.status_code == 409
        assert client.get(f"/sources/{source_id}/inventory/scopes", params={"after": "invalid"}).status_code == 422


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server],
                         ids=["compact", "advanced"])
def test_revoked_read_handle_refuses_on_http_and_both_mcp_surfaces(committed, builder):
    store, source_id, _inventory, run, _row = committed
    store.access_enforced = False
    try:
        store.revoke_source_access("demo", source_id, principal_type="user", principal_id="reader")
    finally:
        store.access_enforced = True
    with principal_scope(base.READER), TestClient(build_agent_api(store)) as client:
        assert client.get(f"/sources/{source_id}/inventory/scopes").status_code == 404
        assert client.post(f"/sources/{source_id}/inventory/{run['id']}/select", json=_body(run)).status_code == 404
    async def refuse():
        from mcp.server.mcpserver.exceptions import ToolError

        with principal_scope(base.READER), pytest.raises(ToolError, match="not found"):
            await builder(store).call_tool("get_inventory", {
                "connection_id": source_id, "operation": "select",
                "payload": {"inventory_id": run["id"], **_body(run)["payload"]}})
    asyncio.run(refuse())
