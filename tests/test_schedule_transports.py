"""Harnesses configure recurrence without adding credentials or approval powers."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore

CONFIG = {"cadence": "daily", "timezone": "America/Chicago", "hour": 2, "minute": 15,
          "freshness_max_age_seconds": 86400}


@pytest.mark.parametrize("surface", ["browser", "agent"])
def test_schedule_http_validation_pause_and_enablement(tmp_path, surface):
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    try:
        source = store.create_connection("demo", kind="local_files", name="Customer scope", config={})
        if surface == "browser":
            app = FastAPI()
            app.include_router(create_knowledge_router(store=store, allowed_kinds=frozenset({"local_files"})))
            path = f"/api/knowledge/connections/{source['id']}/refresh-schedule"
        else:
            app = build_agent_api(store, enable_source_management=True)
            path = f"/sources/{source['id']}/refresh-schedule"
        with TestClient(app) as client:
            assert client.get(path).json()["schedule"] is None
            result = client.put(path, json={"config": CONFIG})
            assert result.status_code == 200
            schedule = result.json()["schedule"]
            assert schedule["next_run_at"] is not None and schedule["freshness"]["state"] == "unknown"
            assert client.put(path, json={"config": CONFIG, "paused": "false"}).status_code == 422
            assert client.put(path, json={"config": {**CONFIG, "minute": True}}).status_code == 422
            assert client.put(path, json={"config": CONFIG, "unexpected": 1}).status_code == 422
            paused = client.put(path, json={"config": CONFIG, "paused": True}).json()["schedule"]
            assert paused["paused"] is True and paused["next_run_at"] is None
            assert client.get(path).json()["schedule"]["id"] == schedule["id"]
        disabled = build_agent_api(store, enable_source_management=False)
        with TestClient(disabled) as client:
            assert client.put(f"/sources/{source['id']}/refresh-schedule", json={"config": CONFIG}).status_code == 404
        forbidden = build_agent_api(store, enable_source_management=True, allowed_kinds=frozenset())
        with TestClient(forbidden) as client:
            assert client.put(f"/sources/{source['id']}/refresh-schedule", json={"config": CONFIG}).status_code == 403
    finally:
        store.close()


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server])
def test_schedule_mcp_roundtrip_and_strict_boolean(tmp_path, builder):
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Customer scope", config={})
    async def check():
        disabled = builder(store)
        assert not {"get_refresh_schedule", "set_refresh_schedule"}.intersection(
            tool.name for tool in await disabled.list_tools())
        enabled = builder(store, enable_source_management=True)
        args = {"connection_id": source["id"], "config": CONFIG}
        response = await enabled.call_tool("set_refresh_schedule", args)
        assert response.structured_content["schedule"]["paused"] is False
        with pytest.raises(ToolError, match="valid boolean"):
            await enabled.call_tool("set_refresh_schedule", {**args, "paused": "false"})
        assert (await enabled.call_tool("set_refresh_schedule", {**args, "paused": True})).structured_content["schedule"]["next_run_at"] is None
        assert (await enabled.call_tool("get_refresh_schedule", {"connection_id": source["id"]})).structured_content["schedule"]["paused"] is True
    try:
        asyncio.run(check())
    finally:
        store.close()
