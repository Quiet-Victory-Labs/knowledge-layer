"""Shared receipts cross independent HTTP/MCP stores with current authority.

The installed MCP client uses real ASGI HTTP envelopes and bearer checks.
Disposable fixtures qualify tool behavior, not a customer's IdP or cloud.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.context_receipts import CONFIG_NAME
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.mcp_transport import McpEnvelopeBoundary
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore

spec = importlib.util.spec_from_file_location("receipt_transport_example",
    Path(__file__).resolve().parents[1] / "examples" / "enterprise_consumer.py")
assert spec and spec.loader
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


@pytest.fixture
def company(tmp_path, monkeypatch):
    key = secrets.token_urlsafe(32)
    payload = {"version": 1, "registry_id": "wire-fixture", "active_key_id": "initial",
               "keys": [{"id": "initial", "secret": key}]}
    path = tmp_path / "private-keys"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    monkeypatch.delenv(CONFIG_NAME, raising=False)
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(path))
    with example.enterprise_fixture() as fixture:
        peer = KnowledgeStore(database_url=f"sqlite:///{fixture['root'] / 'registry.db'}",
                              local_files_root=fixture["root"], initialize_schema=False)
        try:
            yield {**fixture, "peer": peer, "path": path, "key": key, "payload": payload,
                   "token": secrets.token_hex(32),
                   "scope": {"query": "Revenue", "connection_ids": [
                       binding["connection_id"] for binding in fixture["bindings"].values()]}}
        finally:
            peer.close()


def _http(store, token):
    return TestClient(AgentBearerBoundary(build_agent_api(store), token),
                      headers={"Authorization": "Bearer " + token})


def test_http_issues_here_revalidates_there_and_key_failure_is_safe(company):
    with _http(company["store"], company["token"]) as first, _http(company["peer"], company["token"]) as second:
        issued = first.post("/task-context", json=company["scope"])
        assert issued.status_code == 200
        packet = issued.json()
        assert packet["receipt_route"] == "shared_registry_replicas"
        assert packet["receipt"]["schema_version"] == "context-receipt-v2"
        receipt = packet["receipt"]
        checked = second.post("/task-context/revalidate", json=receipt)
        assert checked.status_code == 200 and checked.json()["status"] == "current"
        assert checked.json()["source_permissions_verified"] is False
        assert company["key"] not in issued.text + checked.text
        company["path"].unlink()
        # Authentication denial precedes a now-unavailable signing configuration.
        denied = second.post("/task-context", json=company["scope"], headers={"Authorization": "Bearer incorrect"})
        assert denied.status_code == 401
        for route, request in (("/task-context", company["scope"]), ("/task-context/revalidate", receipt)):
            failed = second.post(route, json=request)
            assert failed.status_code == 409
            assert failed.json() == {"detail": "Context receipt key configuration is unavailable; contact the operator"}
            assert company["key"] not in failed.text and str(company["path"]) not in failed.text


@asynccontextmanager
async def _mcp(store, builder, token):
    server = builder(store)
    app = AgentBearerBoundary(McpEnvelopeBoundary(server.streamable_http_app(
        streamable_http_path="/", json_response=True, stateless_http=True)), token)
    async with (
        server.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                          headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=5) as http,
        Client(streamable_http_client("http://127.0.0.1:8273/", http_client=http),
               mode="2026-07-28", cache=None, read_timeout_seconds=5) as client,
    ):
        yield client


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server], ids=["compact", "advanced"])
def test_actual_sdk_wire_crosses_replica_rotates_and_refuses_withdrawal(company, builder):
    source_hashes = {unit: hashlib.sha256(binding["database"].read_bytes()).hexdigest()
                     for unit, binding in company["bindings"].items()}
    async def consume():
        async with (_mcp(company["store"], builder, company["token"]) as first,
                    _mcp(company["peer"], builder, company["token"]) as second):
            result = await first.call_tool("get_task_context", company["scope"])
            assert not result.is_error
            receipt = result.structured_content["receipt"]
            checked = await second.call_tool("revalidate_task_context", {"receipt": receipt})
            assert not checked.is_error and checked.structured_content["status"] == "current"
            fresh_key = secrets.token_urlsafe(32)
            company["payload"]["keys"].append({"id": "rotated", "secret": fresh_key})
            company["payload"]["active_key_id"] = "rotated"
            replacement = company["path"].with_name("replacement-keys")
            replacement.write_text(json.dumps(company["payload"]))
            replacement.chmod(0o600)
            os.replace(replacement, company["path"])
            renewed = await second.call_tool("revalidate_task_context", {"receipt": receipt})
            assert not renewed.is_error
            renewed_receipt = renewed.structured_content["receipt"]
            assert renewed_receipt["key_id"] == "rotated"
            assert not (await first.call_tool("revalidate_task_context", {"receipt": renewed_receipt})).is_error
            finance = company["bindings"]["finance"]["connection_id"]
            company["store"].revoke_publication("demo", finance, reason="Fixture steward withdrew revenue authority.")
            refused = await second.call_tool("revalidate_task_context", {"receipt": renewed_receipt})
            assert refused.is_error and refused.structured_content is None
            assert "Context changed" in refused.content[0].text
            for value in (result, checked, renewed, refused):
                wire = value.model_dump_json()
                assert company["key"] not in wire and fresh_key not in wire
    asyncio.run(consume())
    assert source_hashes == {unit: hashlib.sha256(binding["database"].read_bytes()).hexdigest()
                             for unit, binding in company["bindings"].items()}


def test_fresh_interpreter_accepts_receipt_then_observes_withdrawal(company, tmp_path):
    with _http(company["store"], company["token"]) as first:
        receipt = first.post("/task-context", json=company["scope"]).json()["receipt"]
    held = tmp_path / "private-receipt"
    held.write_text(json.dumps(receipt))
    held.chmod(0o600)
    child = """
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.store import KnowledgeStore
root, held = Path(sys.argv[1]), Path(sys.argv[2])
store = KnowledgeStore(database_url='sqlite:///' + str(root / 'registry.db'),
                       local_files_root=root, initialize_schema=False)
try:
    with TestClient(build_agent_api(store)) as client:
        response = client.post('/task-context/revalidate', json=json.loads(held.read_text()))
    print(json.dumps({'http_status': response.status_code,
                      'current': response.json().get('status') == 'current'}))
finally:
    store.close()
"""
    env = {"PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           CONFIG_NAME + "_FILE": str(company["path"])}
    def independent_request():
        result = subprocess.run([sys.executable, "-c", child, str(company["root"]), str(held)],
                                env=env, capture_output=True, timeout=15, check=True, text=True)
        assert len(result.stdout) < 256
        return json.loads(result.stdout)
    assert independent_request() == {"http_status": 200, "current": True}
    finance = company["bindings"]["finance"]["connection_id"]
    company["store"].revoke_publication("demo", finance, reason="Steward withdrew authority before a fresh interpreter request.")
    assert independent_request() == {"http_status": 409, "current": False}
