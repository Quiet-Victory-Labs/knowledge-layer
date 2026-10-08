"""The installed SDK consumes exact discovery over authenticated ASGI HTTP MCP."""

import asyncio
import importlib.util
import secrets
from pathlib import Path

import pytest

from knowledge_layer.access import principal_scope
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = _load("inventory_wire_fixture", "test_inventory_selection_transport.py")
wire = _load("inventory_sdk_wire", "test_context_receipt_transport.py")
registry = transport.registry
committed = transport.committed


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server],
                         ids=["compact", "advanced"])
def test_sdk_reads_exact_selection_and_refuses_stale_and_revoked_handles(committed, builder):
    store, source_id, inventory, _run, row = committed
    before = transport.fixtures._state(store)

    async def consume():
        with principal_scope(transport.base.READER):
            async with wire._mcp(store, builder, secrets.token_hex(32)) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert tools["get_inventory"].annotations.read_only_hint is True
                assert "stage_inventory" not in tools
                scopes = await client.call_tool("get_inventory", {
                    "connection_id": source_id, "operation": "scopes", "payload": {"limit": 1}})
                assert not scopes.is_error
                selected_scope = scopes.structured_content["items"][0]
                request = {"connection_id": source_id, "operation": "select", "payload": {
                    "inventory_id": selected_scope["id"],
                    "expected_manifest_digest": selected_scope["manifest_digest"],
                    "entity_ids": [row["id"]]}}
                result = await client.call_tool("get_inventory", request)
                assert not result.is_error
                packet = result.structured_content
                assert packet["entities"][0]["payload"] == row["payload"]
                assert [proof["id"] for proof in packet["evidence"]] == ["exact-proof"]
                assert packet["source_text_authority"] == "untrusted_evidence"
                assert packet["meaning_certified"] is packet["agent_ready"] is False
                assert packet["source_reads_executed"] is False
                assert transport.fixtures._state(store) == before

                # The native SDK holds an old selection while the source manager
                # commits a complete successor in the same synthetic registry.
                with principal_scope(transport.base.OWNER):
                    changed = transport.base.entity(refs=("exact-proof",))
                    changed["payload"]["definition"] = "Replacement discovery remains unapproved."
                    transport.fixtures._complete(inventory, source_id, entities=[changed],
                                                 evidence=[transport.base.evidence("exact-proof")])
                stale = await client.call_tool("get_inventory", request)
                assert stale.is_error and stale.structured_content is None
                assert "current manifest changed" in stale.content[0].text
                assert row["payload"]["definition"] not in stale.model_dump_json()

                store.access_enforced = False
                try:
                    store.revoke_source_access("demo", source_id, principal_type="user",
                                               principal_id=transport.base.READER.subject)
                finally:
                    store.access_enforced = True
                denied = await client.call_tool("get_inventory", {
                    "connection_id": source_id, "operation": "scopes", "payload": {}})
                assert denied.is_error and denied.structured_content is None
                assert "Inventory source was not found" in denied.content[0].text
                assert row["payload"]["definition"] not in denied.model_dump_json()
                assert "exact-proof" not in denied.model_dump_json()

    asyncio.run(consume())
