"""MCP 2026-07-28 wire qualification through the installed SDK and ASGI auth.

SQLite holds disposable metadata; JWT signatures are verified with an offline
test key. No IdP network, source reads, service, or customer client is qualified.
"""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from types import SimpleNamespace

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client

from knowledge_layer.gateway import create_gateway
from knowledge_layer.mcp_transport import McpEnvelopeBoundary
from knowledge_layer.platform import create_platform
from knowledge_layer.store import KnowledgeStore

VERSION = "2026-07-28"
ORIGIN = "https://knowledge.example.test"
ISSUER = "https://id.example.test/company"
AUDIENCE = "knowledge-agent"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_INFO = "io.modelcontextprotocol/clientInfo"
ROUTES = ("/agent/mcp/", "/agent/advanced-mcp/")


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key, **overrides):
    claims = {"sub": "modern-reader", "iss": ISSUER, "aud": AUDIENCE,
              "exp": int(time.time()) + 120, "groups": ["knowledge-users"],
              "scope": "knowledge.agent", **overrides}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "offline-test-key"})


@pytest.fixture
def platform(monkeypatch, tmp_path, signing_key):
    settings = {
        "KNOWLEDGE_AUTH_MODE": "oidc", "KNOWLEDGE_ENABLED_KINDS": "local_files",
        "KNOWLEDGE_AGENT_SOURCE_MANAGEMENT": "0",
        "KNOWLEDGE_AGENT_ALLOWED_KINDS": "local_files",
        "KNOWLEDGE_OIDC_ISSUER": ISSUER, "KNOWLEDGE_OIDC_CLIENT_ID": "knowledge-web",
        "KNOWLEDGE_OIDC_CLIENT_SECRET": "offline-test-client-secret",  # pragma: allowlist secret - offline fixture value
        "KNOWLEDGE_OIDC_AUDIENCE": AUDIENCE,
        "KNOWLEDGE_OIDC_JWKS_URI": ISSUER + "/jwks",
        "KNOWLEDGE_OIDC_SESSION_KEY": "offline-test-long-session-signing-key-123456789",
        "KNOWLEDGE_OIDC_ALLOWED_GROUP": "knowledge-users",
        "KNOWLEDGE_OIDC_STEWARD_GROUP": "knowledge-stewards", "KNOWLEDGE_PUBLIC_URL": ORIGIN,
        "KNOWLEDGE_SOURCE_POLICY": json.dumps({"version": 1, "sources": [{
            "tenant_id": "demo", "kind": "local_files",
            "selector": {"path": ".", "include": "**/*"},
            "allowed_subjects": ["modern-reader"], "allowed_groups": []}]}),
    }
    for name, value in settings.items():
        monkeypatch.delenv(name + "_FILE", raising=False)
        monkeypatch.setenv(name, value)
    # Transport/auth qualification has no PostgreSQL migration claim. The
    # current registry models create isolated SQLite metadata for these tests.
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt",
                        lambda _self, _token: SimpleNamespace(key=signing_key.public_key()))
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    visible = store.create_connection("demo", kind="local_files", name="Visible fixture", config={})
    denied = store.create_connection("demo", kind="local_files", name="Hidden fixture must not leak", config={})
    store.grant_source_access("demo", visible["id"], principal_type="user",
                              principal_id="modern-reader", role="read")
    app = create_platform(store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        yield SimpleNamespace(app=app, client=client, store=store,
                              token=_token(signing_key), visible=visible["id"], denied=denied["id"])
    store.close()


def _request(method="tools/list", *, name=None, arguments=None):
    params = {"_meta": {META_VERSION: VERSION, META_INFO: {
        "name": "knowledge-modern-wire-test", "version": "1"}, META_CAPABILITIES: {}}}
    if name is not None:
        params.update(name=name, arguments=arguments or {})
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def _headers(token, body):
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", "MCP-Protocol-Version": VERSION,
               "Mcp-Method": body["method"]}
    if "name" in body["params"]:
        headers["Mcp-Name"] = body["params"]["name"]
    return headers


@pytest.mark.parametrize("route", ROUTES)
def test_modern_wire_lists_tools_and_calls_capabilities_without_initialize(platform, route):
    listing = _request()
    response = platform.client.post(route, headers=_headers(platform.token, listing), json=listing)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {"get_capabilities", "get_inventory", "get_maintenance_work"} <= names
    # Draft curation is a permitted metadata operation. Source management,
    # scheduler mutations and steward approval/publication remain unavailable.
    assert not names & {"connect_source", "refresh_source", "create_source", "set_refresh_schedule",
                        "stage_inventory", "publish_source", "publish_guidance", "revoke_guidance",
                        "decide_proposal", "execute_sql"}
    body = _request("tools/call", name="get_capabilities")
    response = platform.client.post(route, headers=_headers(platform.token, body), json=body)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError", False) is False
    assert result["structuredContent"]["schema_version"] == "knowledge-capabilities-v1"
    assert VERSION in result["structuredContent"]["versions"]["mcp"]["sdk_supported_protocols"]
    assert "mcp-session-id" not in response.headers


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("missing", ("_meta", META_VERSION, META_CAPABILITIES))
def test_modern_required_metadata_missing_is_protocol_refusal(platform, route, missing):
    body = _request()
    if missing == "_meta":
        body["params"].pop("_meta")
    else:
        body["params"]["_meta"].pop(missing)
    response = platform.client.post(route, headers=_headers(platform.token, body), json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == -32602


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("header", ("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"))
@pytest.mark.parametrize("changed", (False, True), ids=("missing", "mismatched"))
def test_modern_mirrored_headers_must_match_body(platform, route, header, changed):
    body = _request("tools/call", name="get_capabilities")
    headers = _headers(platform.token, body)
    if changed:
        headers[header] = {"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/list",
                           "Mcp-Name": "get_workspace"}[header]
    else:
        headers.pop(header)
    response = platform.client.post(route, headers=headers, json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == -32020


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("unauthorized", ("missing", "malformed", "wrong_audience", "missing_scope"))
def test_modern_oidc_auth_denial_precedes_tool_dispatch(platform, signing_key, route, unauthorized):
    body = _request("tools/call", name="get_capabilities")
    headers = _headers(platform.token, body)
    if unauthorized == "missing":
        headers.pop("Authorization")
    elif unauthorized == "malformed":
        headers["Authorization"] = "Bearer invalid-jwt"
    else:
        overrides = {"aud": "another-app"} if unauthorized == "wrong_audience" else {"scope": "profile"}
        headers["Authorization"] = "Bearer " + _token(signing_key, **overrides)
    response = platform.client.post(route, headers=headers, json=body)
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
    assert "result" not in response.json()


@pytest.mark.parametrize("route", ROUTES)
def test_modern_denied_source_is_tool_error_without_hidden_content(platform, route):
    name = "get_contract" if route == "/agent/mcp/" else "get_semantic_contract"
    arguments = {"connection_id": platform.denied}
    if name == "get_contract":
        arguments["asset_id"] = "hidden-fixture-asset"
    body = _request("tools/call", name=name, arguments=arguments)
    response = platform.client.post(route, headers=_headers(platform.token, body), json=body)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert "Hidden fixture must not leak" not in response.text
    assert platform.denied not in response.text


@pytest.mark.parametrize("route", ROUTES)
def test_installed_modern_client_uses_actual_asgi_http_and_per_request_metadata(platform, route):
    observed = []

    async def record(request):
        body = json.loads(await request.aread())
        observed.append({"method": body["method"], "params": deepcopy(body.get("params", {})),
                         "version": request.headers.get("MCP-Protocol-Version"),
                         "method_header": request.headers.get("Mcp-Method"),
                         "name_header": request.headers.get("Mcp-Name")})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=platform.app),
            headers={"Authorization": "Bearer " + platform.token}, timeout=5,
            event_hooks={"request": [record]}, trust_env=False) as http:
            transport = streamable_http_client(ORIGIN + route, http_client=http)
            async with Client(transport, mode=VERSION, cache=None, read_timeout_seconds=5,
                client_info=types.Implementation(name="knowledge-sdk-wire-test", version="1")) as client:
                assert client.protocol_version == VERSION
                assert client.session.initialize_result is None
                listing = await client.list_tools()
                assert "get_capabilities" in {tool.name for tool in listing.tools}
                result = await client.call_tool("get_capabilities", {})
                assert result.is_error is False
                assert result.structured_content["schema_version"] == "knowledge-capabilities-v1"
                name = "get_contract" if route == "/agent/mcp/" else "get_semantic_contract"
                arguments = {"connection_id": platform.denied}
                if name == "get_contract":
                    arguments["asset_id"] = "hidden-fixture-asset"
                denied = await client.call_tool(name, arguments)
                assert denied.is_error is True
                assert platform.denied not in json.dumps(denied.model_dump(mode="json"))

    asyncio.run(run())
    assert {item["method"] for item in observed} == {"tools/list", "tools/call"}
    assert not {"initialize", "notifications/initialized", "server/discover"} & {
        item["method"] for item in observed}
    for item in observed:
        assert item["version"] == item["params"]["_meta"][META_VERSION] == VERSION
        assert item["method_header"] == item["method"]
        assert item["params"]["_meta"][META_CAPABILITIES] == {}
        assert item["params"]["_meta"][META_INFO]["name"] == "knowledge-sdk-wire-test"
        if item["method"] == "tools/call":
            assert item["name_header"] == item["params"]["name"]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("authorized", (False, True), ids=("unauthenticated", "authenticated"))
def test_actual_envelope_overflow_refused_only_after_authentication(platform, route, authorized):
    body = _request("tools/call", name="get_capabilities")
    body["params"]["_meta"]["padding"] = "x" * (1024 * 1024)
    headers = _headers(platform.token, body)
    if not authorized:
        headers.pop("Authorization")
    response = platform.client.post(route, headers=headers, json=body)
    assert response.status_code == (413 if authorized else 401)
    assert "result" not in response.json()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("declared_length", (None, "1"), ids=("chunked", "false_small_content_length"))
def test_actual_asgi_chunks_cannot_bypass_envelope_byte_limit(platform, route, declared_length):
    """Each event fits; their sum exceeds the bound before the third event."""
    body = _request("tools/call", name="get_capabilities")
    headers = _headers(platform.token, body)
    headers["Host"] = "knowledge.example.test"
    headers["Transfer-Encoding"] = "chunked"
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    sent, received = [], []
    chunks = iter((b"x" * (512 * 1024), b"x" * (512 * 1024 + 1)))

    async def receive():
        chunk = next(chunks, None)
        assert chunk is not None, "The guard continued reading after actual-byte overflow"
        received.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "https", "path": route, "raw_path": route.encode(), "root_path": "",
             "query_string": b"", "server": ("knowledge.example.test", 443), "client": ("127.0.0.1", 1000),
             "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()]}
    asyncio.run(platform.app(scope, receive, send))
    assert [item["status"] for item in sent if item["type"] == "http.response.start"] == [413]
    assert received == [512 * 1024, 512 * 1024 + 1]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("duplicate", (VERSION, "2025-11-25"), ids=("same_version", "different_version"))
def test_duplicate_protocol_headers_cannot_select_another_dispatcher(platform, route, duplicate):
    body = _request("tools/call", name="get_capabilities")
    headers = [*_headers(platform.token, body).items(), ("mcp-protocol-version", duplicate)]
    response = platform.client.post(route, headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


@pytest.mark.parametrize("route", ROUTES)
def test_legacy_client_still_negotiates_and_reads_through_same_boundary(platform, route):
    observed = []

    async def record(request):
        body = json.loads(await request.aread())
        observed.append(body)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=platform.app),
            headers={"Authorization": "Bearer " + platform.token}, timeout=5,
            event_hooks={"request": [record]}, trust_env=False) as http:
            transport = streamable_http_client(ORIGIN + route, http_client=http)
            async with Client(transport, mode="legacy", cache=None, read_timeout_seconds=5) as client:
                assert client.protocol_version == "2025-11-25"
                assert client.session.initialize_result is not None
                listing = await client.list_tools()
                assert "get_capabilities" in {tool.name for tool in listing.tools}
                result = await client.call_tool("get_capabilities", {})
                assert not result.is_error
                assert result.structured_content["schema_version"] == "knowledge-capabilities-v1"

    asyncio.run(run())
    assert observed[0]["method"] == "initialize"
    assert {"notifications/initialized", "tools/list", "tools/call"} <= {
        item["method"] for item in observed}
    assert all(META_VERSION not in (item.get("params", {}).get("_meta") or {}) for item in observed)


def test_exact_limit_replays_original_utf8_bytes_without_normalizing_json():
    """An exact-bound body reaches downstream intact, including whitespace."""
    base = b' {"params":{"padding":"caf\xc3\xa9", "_meta":{"' + META_VERSION.encode() + b'":"' + VERSION.encode() + b'"}}}\n'
    filler = b" " * (1024 * 1024 - len(base))
    payload = base + filler
    assert len(payload) == 1024 * 1024
    captured, sent = [], []
    events = iter(({"type": "http.request", "body": payload[:31], "more_body": True},
                   {"type": "http.request", "body": payload[31:], "more_body": False}))

    async def scripted_receive():
        return next(events)

    async def downstream(_scope, read, send):
        message = await read()
        captured.append(message)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [(b"mcp-protocol-version", VERSION.encode())]}
    asyncio.run(McpEnvelopeBoundary(downstream)(scope, scripted_receive, send))
    assert captured == [{"type": "http.request", "body": payload, "more_body": False}]
    assert sent[-1]["body"] == b"ok"


@pytest.mark.parametrize("header", (None, "2025-11-25"), ids=("missing", "downgraded"))
def test_gateway_also_refuses_modern_envelope_downgrade(header):
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        gateway = create_gateway(store=store, agent_token="offline-gateway-token")
        with TestClient(gateway, base_url="http://127.0.0.1:4173") as client:
            body = _request("tools/call", name="get_capabilities")
            headers = _headers("offline-gateway-token", body)
            if header is None:
                headers.pop("MCP-Protocol-Version")
            else:
                headers["MCP-Protocol-Version"] = header
            response = client.post("/agent/mcp/", headers=headers, json=body)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32020
            valid = client.post("/agent/mcp/", headers=_headers("offline-gateway-token", body), json=body)
            assert valid.status_code == 200
            assert not valid.json()["result"].get("isError", False)
            body["params"]["_meta"]["padding"] = "x" * (1024 * 1024)
            assert client.post("/agent/mcp/", json=body).status_code == 401
    finally:
        store.close()
