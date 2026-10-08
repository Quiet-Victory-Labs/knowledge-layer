"""The real SDK tool surface must satisfy the gateway drill after expansion."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore

_spec = importlib.util.spec_from_file_location(
    "gateway_drill_contract", Path(__file__).parents[1] / "scripts/drill_local_gateway.py")
_drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_drill)
_quick_tool_count = _drill._quick_tool_count


def test_gateway_drill_accepts_actual_expanded_sdk_listing():
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        server = build_quick_mcp_server(store, enable_source_management=True)
        tools = asyncio.run(server.list_tools())
        listing = {"result": {"tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools]}}
        assert _quick_tool_count(200, listing) == len(tools)
        assert len(tools) > 9
        # A missing consumer primitive fails even with many other valid tools.
        listing["result"]["tools"] = [tool for tool in listing["result"]["tools"]
                                      if tool["name"] != "get_task_context"]
        with pytest.raises(RuntimeError, match="tool contract"):
            _quick_tool_count(200, listing)
    finally:
        store.close()


@pytest.mark.parametrize("status,response", [
    (401, {"result": {"tools": []}}), (200, {}), (200, {"result": []}),
    (200, {"result": {"tools": []}}), (200, {"result": {"tools": [{"name": []}]}}),
    (200, {"result": {"tools": [{"name": "get_workspace"}, {"name": "get_workspace"}]}}),
])
def test_gateway_drill_refuses_unavailable_or_malformed_tool_listing(status, response):
    with pytest.raises(RuntimeError, match="tool contract"):
        _quick_tool_count(status, response)
