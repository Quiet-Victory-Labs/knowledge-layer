"""Real harness transports stage exact bounded discovery, never publication."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.inventory_transport import inventory_operation
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeStore, KnowledgeValidationError

BEGIN = {"scope": {"namespace": "finance", "selectors": ["orders"]},
         "policy": {"id": "metadata-only", "revision": None},
         "watermark": {"kind": "unknown", "value": None},
         "limits": {"max_pages": 2, "max_entities": 2, "max_evidence": 2, "max_total_bytes": 8192}}


@pytest.fixture
def registry(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'transport.db'}")
    source = store.create_connection("demo", kind="local_files", name="Source", config={})
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="owner", role="manage")
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="reader", role="read")
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["owner", "reader"], "allowed_groups": [],
    }]}))
    store.access_enforced = True
    yield store, source["id"]
    store.close()


def owner():
    return principal_scope(Principal("owner", frozenset(), frozenset({"knowledge.sources.manage"})))


def test_http_stages_complete_empty_and_readers_cannot_mutate(registry):
    store, cid = registry
    client = TestClient(build_agent_api(store, enable_source_management=True))
    prefix = f"/sources/{cid}/inventory"
    with owner():
        run = client.post(prefix + "/begin", json={"payload": BEGIN}).json()
        assert "id" in run
        empty = client.get(prefix + "/current", params={key: run[key] for key in ("scope_digest", "policy_digest")})
        assert empty.json()["status"] == "not_established"
        payload = {"inventory_id": run["id"], "page_index": 0, "cursor": None, "next_cursor": None,
                   "entities": [], "evidence": [], **{key: run[key] for key in ("scope_digest", "policy_digest", "watermark_digest")}}
        page = client.post(prefix + "/append", json={"payload": payload})
        assert page.status_code == 200, page.text
        run = page.json()
        committed = client.post(prefix + "/commit", json={"payload": {"inventory_id": run["id"],
            "completion": {"listing_complete": True, "pages": 1, "entities": 0, "evidence": 0,
                           "manifest_digest": run["manifest_digest"]}}})
        assert committed.status_code == 200, committed.text
        assert committed.json()["compatibility"]["agent_ready"] is False
        assert store.get_connection("demo", cid)["last_complete_refresh_at"] is None
    with principal_scope(Principal("reader", frozenset())):
        result = client.get(prefix + f"/{run['id']}/page")
        assert result.status_code == 200 and result.json()["items"] == []
        assert client.post(prefix + "/begin", json={"payload": BEGIN}).status_code == 403
    with principal_scope(Principal("stranger", frozenset())):
        assert client.get(prefix + f"/{run['id']}").status_code == 404


def test_disabling_management_omits_mutation_routes_and_tools(registry):
    store, cid = registry
    with owner(), TestClient(build_agent_api(store)) as client:
        assert client.post(f"/sources/{cid}/inventory/begin", json={"payload": BEGIN}).status_code == 405
    async def run():
        names = {tool.name for tool in await build_quick_mcp_server(store).list_tools()}
        assert "get_inventory" in names and "stage_inventory" not in names
    asyncio.run(run())


def test_callers_cannot_inject_tenant_claim_or_publication_authority(registry):
    store, cid = registry
    with owner():
        for value in ({**BEGIN, "claim": {"attempt": 1}}, {**BEGIN, "tenant_id": "other"},
                      {**BEGIN, "agent_ready": True}):
            with pytest.raises(KnowledgeValidationError, match="unsupported"):
                inventory_operation(store, cid, "begin", value, allow_writes=True)


def test_mcp_stages_exact_pages_and_invalid_calls_are_expected_errors(registry):
    store, cid = registry
    async def run():
        from mcp.server.mcpserver.exceptions import ToolError
        server = build_quick_mcp_server(store, enable_source_management=True)
        with owner():
            result = await server.call_tool("stage_inventory", {"connection_id": cid, "operation": "begin", "payload": BEGIN})
            assert not result.is_error and result.structured_content["compatibility"]["meaning_certified"] is False
            with pytest.raises(ToolError, match="required"):
                await server.call_tool("stage_inventory", {"connection_id": cid, "operation": "append", "payload": {}})
    asyncio.run(run())


def test_inventory_transport_never_materializes_legacy_semantic_snapshot(registry, monkeypatch):
    store, cid = registry
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Inventory transport projected an aggregate snapshot")
    monkeypatch.setattr(store, "get_connection", forbidden)
    monkeypatch.setattr(store, "get_snapshot", forbidden)
    with owner():
        run = inventory_operation(store, cid, "begin", BEGIN, allow_writes=True, allowed_kinds=frozenset({"local_files"}))
        result = inventory_operation(store, cid, "status", {"inventory_id": run["id"]})
        assert result["id"] == run["id"] and result["status"] == "staging"
