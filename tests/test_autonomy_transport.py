"""Autonomy transports expose bounded delegation, never approval to agents.

These tests use disposable SQLite metadata and offline signed JWTs; production
PostgreSQL transaction behavior and a real identity provider are separate gates.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.autonomy_transport import autonomy_operation
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.platform import create_platform
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeStore, KnowledgeValidationError

TOOLS = {"get_autonomy_policy", "apply_autonomy_policy"}
MANAGER = Principal("delegate", frozenset({"users"}), frozenset({
    "knowledge.agent", "knowledge.sources.manage", "knowledge.autonomy",
}))
STEWARD = Principal("steward", frozenset({"users", "stewards"}))
READER = Principal("reader", frozenset({"users"}))


def _policy():
    return {
        "schema_version": "knowledge-autonomy-policy-v1",
        "action": "publish_reviewed_unchanged", "subject": "delegate",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "max_publications": 2, "purpose": "Publish only this unchanged owner-reviewed source meaning.",
    }


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\nO-2,0\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders fixture",
                                     config={"path": ".", "include": "*.csv"})
    hidden = store.create_connection("demo", kind="local_files", name="Hidden source fixture",
                                     config={"path": ".", "include": "*.csv"})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    evidence = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"])
    proposal = store.submit_contract_assertion(
        "demo", source["id"], assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": "One source row per fixture order."}, evidence_ids=[evidence],
        rationale="The local fixture inventory supports this description.", expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve",
                          rationale="The steward independently reviewed the bounded fixture.")
    reviewed = store.get_snapshot("demo", source["id"])
    for subject, role in (("delegate", "manage"), ("steward", "manage"), ("reader", "read")):
        store.grant_source_access("demo", source["id"], principal_type="user", principal_id=subject, role=role)
    policy = {"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "*.csv"},
        "allowed_subjects": ["delegate", "steward", "reader"], "allowed_groups": [],
    }]}
    store.source_policy = SourcePolicy.from_json(json.dumps(policy))
    store.access_enforced = True
    store.source_admin_group = "stewards"
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store, enable_autonomy=True))
    with TestClient(app) as browser:
        yield SimpleNamespace(store=store, cid=source["id"], hidden=hidden["id"], snapshot=reviewed,
                              browser=browser, policy=policy)
    store.close()


def _draft(registry):
    with principal_scope(MANAGER):
        response = registry.browser.post(f"/api/knowledge/connections/{registry.cid}/autonomy/draft",
                                          json={"payload": _policy()})
    assert response.status_code == 201, response.json()
    return response.json()


def _approved(registry):
    row = _draft(registry)
    with principal_scope(STEWARD):
        response = registry.browser.post(f"/api/knowledge/connections/{registry.cid}/autonomy/approve", json={
            "revision": row["revision"], "expected_digest": row["digest"],
            "rationale": "The steward accepts this exact bounded delegation.",
        })
    assert response.status_code == 200, response.json()
    return row


def _apply(registry, row):
    return {"revision": row["revision"], "expected_digest": row["digest"],
            "expected_snapshot_id": registry.snapshot["id"],
            "expected_snapshot_version": registry.snapshot["version"]}


def test_browser_managers_draft_but_only_real_steward_can_approve_and_revoke(registry):
    base = f"/api/knowledge/connections/{registry.cid}/autonomy"
    with principal_scope(READER):
        assert registry.browser.get(base).status_code == 403
        assert registry.browser.post(base + "/draft", json={"payload": _policy()}).status_code == 403
    row = _draft(registry)
    approval = {"revision": row["revision"], "expected_digest": row["digest"],
                "rationale": "The steward reviewed this bounded delegation."}
    withdrawal = {"revision": row["revision"], "expected_digest": row["digest"],
                  "reason": "The steward withdrew this exact delegated authority."}
    with principal_scope(MANAGER):
        assert registry.browser.get(base).json()["state"] == "draft"
        assert registry.browser.post(base + "/approve", json=approval).status_code == 403
    with principal_scope(STEWARD):
        assert registry.browser.post(base + "/approve", json=approval).status_code == 200
    with principal_scope(MANAGER):
        active = registry.browser.get(base).json()
        assert active["state"] == "active" and active["execution_enabled"] is True
        assert registry.browser.post(base + "/revoke", json=withdrawal).status_code == 403
    with principal_scope(STEWARD):
        assert registry.browser.post(base + "/revoke", json=withdrawal).status_code == 200
    with principal_scope(MANAGER):
        assert registry.browser.get(base).json()["state"] == "revoked"


def test_http_default_omits_autonomy_and_opt_in_exposes_only_get_apply(registry):
    row = _approved(registry)
    base = f"/sources/{registry.cid}/autonomy"
    with TestClient(build_agent_api(registry.store)) as disabled, principal_scope(MANAGER):
        assert disabled.get(base).status_code == 404
        assert disabled.post(base + "/apply", json=_apply(registry, row)).status_code == 404
    with TestClient(build_agent_api(registry.store, enable_autonomy=True)) as enabled, principal_scope(MANAGER):
        result = enabled.get(base)
        assert result.status_code == 200 and result.json()["execution_enabled"] is True
        for operation in ("draft", "approve", "revoke"):
            assert enabled.post(base + "/" + operation, json={}).status_code == 404
        applied = enabled.post(base + "/apply", json=_apply(registry, row))
        assert applied.status_code == 200, applied.json()
        assert applied.json()["result"] == "published"
        assert applied.json()["source_executed"] is False
        replay = enabled.post(base + "/apply", json=_apply(registry, row))
        assert replay.json()["result"] == "already_applied"
        assert replay.json()["run"]["id"] == applied.json()["run"]["id"]
    with registry.store.engine.connect() as conn:
        assert conn.execute(select(registry.store.autonomy_runs)).mappings().one()["actor"] == "delegate"


@pytest.mark.parametrize("change", [
    {"revision": True}, {"revision": "1"}, {"revision": 1.0}, {"revision": 2**31},
    {"expected_snapshot_version": False}, {"expected_snapshot_version": "1"},
    {"expected_snapshot_version": 1.0}, {"expected_snapshot_version": 2**31},
    {"expected_digest": "g" * 64}, {"expected_digest": "A" * 64},
    {"expected_snapshot_id": "x" * 129}, {"tenant_id": "other"}, {"subject": "steward"},
    {"execution_enabled": True}, {"enable_autonomy": True}, {"actor": "steward"},
])
def test_agent_apply_refuses_coercion_over_budget_and_caller_authority(registry, change):
    row = _approved(registry)
    with TestClient(build_agent_api(registry.store, enable_autonomy=True)) as client, principal_scope(MANAGER):
        response = client.post(f"/sources/{registry.cid}/autonomy/apply", json={**_apply(registry, row), **change})
        assert response.status_code == 422
    with registry.store.engine.connect() as conn:
        assert conn.execute(select(registry.store.autonomy_runs)).all() == []
        assert conn.execute(select(registry.store.publications)).all() == []


@pytest.mark.parametrize("change,nested", [
    ({"expected_revision": True}, False), ({"expected_revision": "0"}, False),
    ({"tenant_id": "other"}, False), ({"approved_by": "steward"}, False),
    ({"max_publications": True}, True), ({"max_publications": "2"}, True),
    ({"max_publications": 1001}, True), ({"source_executed": True}, True),
    ({"action": "execute_sql"}, True), ({"purpose": "x" * 1001}, True),
])
def test_browser_draft_payload_is_strict_and_bounded(registry, change, nested):
    body = {"payload": {**_policy(), **change}} if nested else {"payload": _policy(), **change}
    with principal_scope(MANAGER):
        response = registry.browser.post(f"/api/knowledge/connections/{registry.cid}/autonomy/draft", json=body)
    assert response.status_code == 422
    with registry.store.engine.connect() as conn:
        assert conn.execute(select(registry.store.autonomy_policies)).all() == []


@pytest.mark.parametrize("revision", ["true", "1.0", "-1", "0", "2147483648", "1" * 40])
def test_http_policy_get_refuses_invalid_revision_without_unbounded_integer_parse(registry, revision):
    with principal_scope(MANAGER):
        response = registry.browser.get(f"/api/knowledge/connections/{registry.cid}/autonomy?revision={revision}")
    assert response.status_code == 422


def test_hidden_source_and_revoked_source_grant_do_not_leak_policy(registry):
    row = _approved(registry)
    with TestClient(build_agent_api(registry.store, enable_autonomy=True)) as client, principal_scope(MANAGER):
        assert client.get(f"/sources/{registry.hidden}/autonomy").status_code == 404
        assert client.post(f"/sources/{registry.hidden}/autonomy/apply", json=_apply(registry, row)).status_code == 404
    # Grant management is a trusted operator action, not a steward HTTP power.
    operator = KnowledgeStore(database_url=str(registry.store.engine.url), initialize_schema=False)
    try:
        operator.revoke_source_access("demo", registry.cid, principal_type="user", principal_id="delegate")
    finally:
        operator.close()
    with TestClient(build_agent_api(registry.store, enable_autonomy=True)) as client, principal_scope(MANAGER):
        denied = client.get(f"/sources/{registry.cid}/autonomy")
        assert denied.status_code == 404
        assert "purpose" not in denied.text and row["digest"] not in denied.text


def test_operation_gate_cannot_be_enabled_by_payload(registry):
    with pytest.raises(KnowledgeAccessDenied, match="not enabled"):
        autonomy_operation(registry.store, registry.cid, "apply", {"enable_execution": True})
    with pytest.raises(KnowledgeValidationError, match="unsupported"):
        autonomy_operation(registry.store, registry.cid, "get", {"tenant_id": "other"})


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server])
def test_mcp_opt_in_adds_exactly_two_tools_and_never_policy_management(registry, builder):
    async def run():
        default = {tool.name for tool in await builder(registry.store).list_tools()}
        enabled = {tool.name for tool in await builder(registry.store, enable_autonomy=True).list_tools()}
        assert enabled - default == TOOLS
        assert default - enabled == set()
        assert not default & TOOLS
        assert not enabled & {"draft_autonomy_policy", "approve_autonomy_policy", "revoke_autonomy_policy"}
    asyncio.run(run())


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server])
def test_mcp_registry_authority_roundtrip_and_strict_binding_errors(registry, builder):
    row = _approved(registry)
    async def run():
        server = builder(registry.store, enable_autonomy=True)
        with principal_scope(MANAGER):
            policy = (await server.call_tool("get_autonomy_policy", {"connection_id": registry.cid})).structured_content
            assert policy["state"] == "active" and policy["execution_enabled"] is True
            for invalid in ({"revision": True}, {"expected_snapshot_version": "1"},
                            {"tenant_id": "other"}, {"subject": "steward"}, {"enable_autonomy": True}):
                with pytest.raises(ToolError):
                    await server.call_tool("apply_autonomy_policy", {
                        "connection_id": registry.cid, "request": {**_apply(registry, row), **invalid},
                    })
            with pytest.raises(ToolError, match="not found"):
                await server.call_tool("get_autonomy_policy", {"connection_id": registry.hidden})
            result = (await server.call_tool("apply_autonomy_policy", {
                "connection_id": registry.cid, "request": _apply(registry, row),
            })).structured_content
            assert result["result"] == "published" and result["source_executed"] is False
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["demo", "token", "dev"])
def test_platform_refuses_autonomy_outside_oidc_before_opening_store(monkeypatch, mode):
    monkeypatch.setenv("KNOWLEDGE_AUTH_MODE", mode)
    monkeypatch.setenv("KNOWLEDGE_AGENT_AUTONOMY", "1")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Autonomy boot refusal occurred after store initialization")
    monkeypatch.setattr("knowledge_layer.platform.KnowledgeStore", forbidden)
    with pytest.raises(ValueError):
        create_platform()


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_oidc_host_propagates_opt_in_and_authenticates_before_registry(registry, monkeypatch, signing_key):
    origin, issuer, audience = "https://knowledge.example.test", "https://id.example.test/company", "knowledge-agent"
    settings = {
        "KNOWLEDGE_AUTH_MODE": "oidc", "KNOWLEDGE_AGENT_AUTONOMY": "1",
        "KNOWLEDGE_AGENT_SOURCE_MANAGEMENT": "0", "KNOWLEDGE_ENABLED_KINDS": "local_files",
        "KNOWLEDGE_AGENT_ALLOWED_KINDS": "local_files", "KNOWLEDGE_OIDC_ISSUER": issuer,
        "KNOWLEDGE_OIDC_CLIENT_ID": "knowledge-web",
        "KNOWLEDGE_OIDC_CLIENT_SECRET": "offline-test-client-secret",  # pragma: allowlist secret - offline fixture value
        "KNOWLEDGE_OIDC_AUDIENCE": audience, "KNOWLEDGE_OIDC_JWKS_URI": issuer + "/jwks",
        "KNOWLEDGE_OIDC_SESSION_KEY": "offline-test-long-session-signing-key-123456789",
        "KNOWLEDGE_OIDC_ALLOWED_GROUP": "users", "KNOWLEDGE_OIDC_STEWARD_GROUP": "stewards",
        "KNOWLEDGE_PUBLIC_URL": origin, "KNOWLEDGE_SOURCE_POLICY": json.dumps(registry.policy),
    }
    for key, value in settings.items():
        monkeypatch.delenv(key + "_FILE", raising=False)
        monkeypatch.setenv(key, value)
    # This test qualifies authentication and host wiring, not a PostgreSQL migration.
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt",
                        lambda _self, _token: SimpleNamespace(key=signing_key.public_key()))
    app = create_platform(store=registry.store)
    token = jwt.encode({"sub": "delegate", "iss": issuer, "aud": audience,
                        "exp": int(time.time()) + 120, "groups": ["users"],
                        "scope": "knowledge.agent knowledge.sources.manage knowledge.autonomy"},
                       signing_key, algorithm="RS256", headers={"kid": "offline-autonomy-key"})
    with TestClient(app, base_url=origin) as client:
        path = f"/agent/api/sources/{registry.cid}/autonomy"
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200 and response.json()["state"] == "not_established"
        for route in ("/agent/mcp/", "/agent/advanced-mcp/"):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "autonomy-transport-test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }}}
            response = client.post(route, json=body, headers={
                "Authorization": "Bearer " + token, "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list", "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
            assert response.status_code == 200, response.text
            names = {tool["name"] for tool in response.json()["result"]["tools"]}
            assert TOOLS <= names
            assert "connect_source" not in names and "approve_autonomy_policy" not in names
