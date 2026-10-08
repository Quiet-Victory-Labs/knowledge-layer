"""Production-mode boundaries deny anonymous, cross-site, and under-scoped access."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from knowledge_layer.access_log import record_source_read, source_read_trace
from knowledge_layer.migrate import SchemaMigrationError
from knowledge_layer.oidc import OidcAgentBoundary, OidcSettings
from knowledge_layer.platform import create_platform
from knowledge_layer.store import KnowledgeStore


def test_inventory_primary_concepts_keep_browser_role_csrf_and_agent_publication_boundaries(monkeypatch, tmp_path):
    from test_inventory_citations import commit_catalog, setup_case

    from knowledge_layer.access import Principal, principal_scope

    settings = _settings(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_POLICY", json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": "catalog", "include": "**/*"},
        "allowed_subjects": [], "allowed_groups": ["knowledge-stewards"],
    }]}))
    case = setup_case(tmp_path)
    store, cid = case["store"], case["catalog"]
    for subject, role in (("owner", "manage"), ("agent-reader", "read")):
        store.grant_source_access("demo", cid, principal_type="user", principal_id=subject, role=role)
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(OidcAgentBoundary, "verify", lambda _self, token: {
        "sub": token, "groups": ["knowledge-users", "knowledge-stewards"], "scope": "knowledge.agent"})
    app = create_platform(store=store)
    with principal_scope(Principal("owner", frozenset({"knowledge-users", "knowledge-stewards"}))):
        commit_catalog(case)
    prefix = f"/api/knowledge/sources/{cid}/inventory-concepts"
    try:
        with TestClient(app, base_url=settings.public_url) as client:
            client.cookies.set("knowledge_session", _cookie(settings, subject="owner",
                              groups=("knowledge-users", "knowledge-stewards")))
            headers = {"X-Knowledge-CSRF": "correct-token", "Origin": settings.public_url}
            proposal = client.post(prefix + "/propose_concept", headers=headers, json={"payload": {
                "evidence_ref": case["ref"], "name": "Revenue", "definition": "Finance revenue excludes refunds.",
                "concept_kind": "business_term", "rationale": "Owner interprets exact fixture catalog evidence."}})
            assert proposal.status_code == 200, proposal.text
            concept = proposal.json()
            payloads = {
                "review_concept": {"concept_id": concept["id"], "decision": "approve"},
                "publish_concept": {"concept_id": concept["id"], "expected_proposal_digest": concept["proposal_digest"],
                                    "rationale": "Steward publishes locally reviewed business meaning."},
                "withdraw_concept": {"concept_id": concept["id"], "reason": "Owner retires this local meaning."},
            }
            for operation, payload in payloads.items():
                client.cookies.set("knowledge_session", _cookie(settings, subject="owner"))
                assert client.post(prefix + "/" + operation, headers=headers, json={"payload": payload}).status_code == 403
                client.cookies.set("knowledge_session", _cookie(settings, subject="owner",
                                  groups=("knowledge-users", "knowledge-stewards")))
                assert client.post(prefix + "/" + operation, json={"payload": payload}).status_code == 403
                assert client.post(prefix + "/" + operation, headers={**headers, "Origin": "https://attacker.test"},
                                   json={"payload": payload}).status_code == 403
            for operation in ("review_concept", "publish_concept"):
                result = client.post(prefix + "/" + operation, headers=headers, json={"payload": payloads[operation]})
                assert result.status_code == 200, result.text
            agent_prefix = f"/agent/api/sources/{cid}/inventory-concepts"
            agent_headers = {"Authorization": "Bearer agent-reader"}
            context = client.get(agent_prefix + "/" + concept["id"], headers=agent_headers)
            assert context.status_code == 200 and context.json()["publication_state"] == "published"
            for operation, payload in payloads.items():
                assert client.post(agent_prefix + "/" + operation, headers=agent_headers,
                                   json={"payload": payload}).status_code == 405
            withdrawn = client.post(prefix + "/withdraw_concept", headers=headers,
                                    json={"payload": payloads["withdraw_concept"]})
            assert withdrawn.status_code == 200
            assert client.get(agent_prefix + "/" + concept["id"], headers=agent_headers).status_code == 404
    finally:
        store.close()


def _settings(monkeypatch):
    values = {
        "KNOWLEDGE_AUTH_MODE": "oidc",
        "KNOWLEDGE_ENABLED_KINDS": "local_files",
        "KNOWLEDGE_OIDC_ISSUER": "https://id.example.test/realms/company",
        "KNOWLEDGE_OIDC_CLIENT_ID": "knowledge-web",
        "KNOWLEDGE_OIDC_CLIENT_SECRET": "client-secret-from-a-mounted-file",
        "KNOWLEDGE_OIDC_AUDIENCE": "knowledge-agent",
        "KNOWLEDGE_OIDC_JWKS_URI": "https://id.example.test/realms/company/jwks",
        "KNOWLEDGE_OIDC_SESSION_KEY": "a-long-random-test-session-signing-key-123456",
        "KNOWLEDGE_OIDC_ALLOWED_GROUP": "knowledge-users",
        "KNOWLEDGE_OIDC_STEWARD_GROUP": "knowledge-stewards",
        "KNOWLEDGE_PUBLIC_URL": "https://knowledge.example.test",
        "KNOWLEDGE_SOURCE_POLICY": json.dumps({"version": 1, "sources": [{
            "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
            "allowed_subjects": [], "allowed_groups": ["knowledge-stewards"],
        }]}),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return OidcSettings()


def test_oidc_login_scopes_allow_provider_specific_minimum(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_OIDC_LOGIN_SCOPES", "openid")
    assert OidcSettings().login_scopes == "openid"
    for value in ("profile email", "openid openid", "openid unknown?scope"):
        monkeypatch.setenv("KNOWLEDGE_OIDC_LOGIN_SCOPES", value)
        with pytest.raises(ValueError, match="LOGIN_SCOPES"):
            OidcSettings()


def test_oidc_browser_session_has_bounded_operator_ttl(monkeypatch):
    _settings(monkeypatch)
    assert OidcSettings().browser_session_seconds == 900
    monkeypatch.setenv("KNOWLEDGE_OIDC_BROWSER_SESSION_SECONDS", "120")
    assert OidcSettings().browser_session_seconds == 120
    for value in ("0", "3601", "not-a-number"):
        monkeypatch.setenv("KNOWLEDGE_OIDC_BROWSER_SESSION_SECONDS", value)
        with pytest.raises(ValueError, match="BROWSER_SESSION_SECONDS"):
            OidcSettings()


def _cookie(settings, *, subject="customer-user", groups=("knowledge-users",), csrf="correct-token"):
    session = {
        "knowledge_identity": {
            "sub": subject, "iss": settings.issuer,
            "groups": list(groups), "expires_at": time.time() + 600,
        },
        "knowledge_csrf": csrf,
    }
    payload = base64.b64encode(json.dumps(session).encode("utf-8"))
    return TimestampSigner(settings.session_key).sign(payload).decode("utf-8")


def test_oidc_access_log_records_verified_actor_and_outcome_without_request_secrets(monkeypatch, tmp_path):
    settings = _settings(monkeypatch)
    events = []
    monkeypatch.setattr("knowledge_layer.access_log.emit_access_event", events.append)
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(
        OidcAgentBoundary, "verify",
        lambda _self, _token: {"sub": "agent-user", "groups": ["knowledge-users"],
                               "scope": "knowledge.agent"},
    )
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    app = create_platform(store=store)
    with TestClient(app, base_url=settings.public_url) as client:
        client.cookies.set("knowledge_session", _cookie(settings, subject="browser-user"))
        assert client.get("/api/knowledge/connections?private=do-not-log").status_code == 200
        assert client.post("/api/knowledge/connections", json={"name": "Denied", "kind": "local_files"},
                           headers={"Origin": settings.public_url}).status_code == 403
        assert client.get("/agent/api/sources").status_code == 401
        assert client.get("/agent/api/sources/conn_missing/status?private=do-not-log",
                          headers={"Authorization": "Bearer do-not-log"}).status_code == 404
        assert client.get("/agent/api/sources",
                          headers={"Authorization": "Bearer do-not-log"}).status_code == 200
        mcp = client.post("/agent/mcp/", headers={
            "Authorization": "Bearer do-not-log",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "get_workspace", "arguments": {}}})
        assert mcp.status_code == 200
    store.close()
    assert [(item["actor"], item["surface"], item["status"]) for item in events] == [
        ("browser-user", "browser", 200),
        ("browser-user", "browser", 403),
        ("agent-user", "agent_http", 404),
        ("agent-user", "agent_http", 200),
        ("agent-user", "agent_mcp", 200),
    ]
    assert events[2]["connection_id"] == "conn_missing"
    assert events[4]["operation"] == "mcp_request"
    assert "do-not-log" not in json.dumps(events)


def test_oidc_source_read_events_follow_grants_across_http_and_mcp(monkeypatch, tmp_path):
    settings = _settings(monkeypatch)
    events = []
    monkeypatch.setattr("knowledge_layer.access_log.emit_access_event", events.append)
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(
        OidcAgentBoundary, "verify",
        lambda _self, token: {"sub": token, "groups": ["knowledge-users"], "scope": "knowledge.agent"},
    )
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source_ids = []
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
        source = store.create_connection("demo", kind="local_files", name=name, config={"path": name})
        store.refresh_connection("demo", source["id"])
        source_ids.append(source["id"])
        store.grant_source_access("demo", source["id"], principal_type="user", principal_id="all-reader", role="read")
    store.grant_source_access("demo", source_ids[0], principal_type="user", principal_id="one-reader", role="read")
    app = create_platform(store=store)

    def source_events() -> list[dict]:
        return [event for event in events if event["event"] == "knowledge.source_read"]

    with TestClient(app, base_url=settings.public_url) as client:
        events.clear()
        assert client.get("/agent/api/sources", headers={"Authorization": "Bearer one-reader"}).status_code == 200
        assert {event["connection_id"] for event in source_events()} == {source_ids[0]}
        assert all(event["actor"] == "one-reader" and event["tenant_id"] == "demo"
                   for event in source_events())

        events.clear()
        assert client.get(f"/agent/api/sources/{source_ids[1]}/status",
                          headers={"Authorization": "Bearer one-reader"}).status_code == 404
        assert source_events() == []

        events.clear()
        assert client.get("/agent/api/document-evidence/search?q=orders",
                          headers={"Authorization": "Bearer all-reader"}).status_code == 200
        assert {event["connection_id"] for event in source_events()} == set(source_ids)

        mcp_headers = {
            "Authorization": "Bearer all-reader", "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18",
        }
        for path, tool_name in (("/agent/mcp/", "get_workspace"),
                                ("/agent/advanced-mcp/", "list_knowledge_sources")):
            events.clear()
            response = client.post(path, headers=mcp_headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool_name, "arguments": {}},
            })
            assert response.status_code == 200
            assert {event["connection_id"] for event in source_events()} == set(source_ids)
            assert all(event["operation"] == "mcp_request" for event in source_events())
    store.close()


def test_source_read_trace_isolated_across_concurrent_requests_and_sync_workers():
    async def request(source_id: str):
        with source_read_trace() as trace:
            await asyncio.to_thread(record_source_read, "demo", source_id)
            await asyncio.sleep(0)
            return trace.sources()

    async def concurrent_requests():
        return await asyncio.gather(request("conn_left"), request("conn_right"))

    left, right = asyncio.run(concurrent_requests())
    assert left == [("demo", "conn_left")]
    assert right == [("demo", "conn_right")]


def test_oidc_browser_requires_session_csrf_and_steward_for_approval(monkeypatch, tmp_path):
    settings = _settings(monkeypatch)
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    app = create_platform(store=store)
    with TestClient(app, base_url=settings.public_url) as client:
        assert client.get("/api/knowledge/connections").status_code == 401
        assert client.post("/api/knowledge/connections", json={"name": "A", "kind": "local_files", "config": {}}).status_code == 401
        client.cookies.set("knowledge_session", _cookie(settings))
        assert client.get("/api/knowledge/connections").status_code == 200
        assert [item["id"] for item in client.get("/api/knowledge/connector-types").json()["connector_types"]] == ["local_files"]
        body = {"name": "A", "kind": "local_files", "config": {}}
        assert client.post("/api/knowledge/connections", json=body).status_code == 403
        assert client.post("/api/knowledge/connections", json=body,
                           headers={"X-Knowledge-CSRF": "wrong"}).status_code == 403
        assert client.post("/api/knowledge/connections", json=body,
                           headers={"X-Knowledge-CSRF": "correct-token", "Origin": "https://attacker.test"}).status_code == 403
        assert client.post("/api/knowledge/connections", json=body,
                           headers={"X-Knowledge-CSRF": "correct-token", "Origin": settings.public_url}).status_code == 403
        client.cookies.set("knowledge_session", _cookie(settings, groups=("knowledge-users", "knowledge-stewards")))
        created = client.post("/api/knowledge/connections", json=body,
                              headers={"X-Knowledge-CSRF": "correct-token", "Origin": settings.public_url})
        assert created.status_code == 201
        assert client.post("/api/knowledge/connections", json={
            "name": "Other folder", "kind": "local_files", "config": {"path": "payroll"},
        }, headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 403
        forbidden_kind = client.post("/api/knowledge/connections", json={
            "name": "Unapproved", "kind": "sql_catalog", "config": {},
        }, headers={"X-Knowledge-CSRF": "correct-token", "Origin": settings.public_url})
        assert forbidden_kind.status_code == 403
        connection_id = created.json()["connection"]["id"]
        assert client.patch(f"/api/knowledge/connections/{connection_id}", json={
            "config": {"path": "payroll"},
        }, headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 403
        publication_path = f"/api/knowledge/connections/{connection_id}/publication"
        assert "discovery_incomplete" in client.get(publication_path).json()["blockers"]
        client.cookies.set("knowledge_session", _cookie(settings))
        assert client.post(publication_path, json={
            "expected_snapshot_id": "not-yet-discovered", "expected_snapshot_version": 1,
            "rationale": "The source owner approves this metadata for agent use.",
        }, headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 403
        denied = client.post(
            f"/api/knowledge/connections/{connection_id}/proposals/example/decision",
            json={"decision": "approve"}, headers={"X-Knowledge-CSRF": "correct-token"},
        )
        assert denied.status_code == 403
        archive_path = f"/api/knowledge/connections/{connection_id}/proposals/archive"
        archive_body = {"proposal_ids": ["example"], "reason": "This draft is superseded."}
        assert client.post(archive_path, json=archive_body).status_code == 403
        assert client.post(archive_path, json=archive_body,
                           headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 403
        client.cookies.set("knowledge_session", _cookie(settings, groups=("knowledge-users", "knowledge-stewards")))
        # A steward passes authorization and reaches the normal missing-proposal check.
        allowed = client.post(
            f"/api/knowledge/connections/{connection_id}/proposals/example/decision",
            json={"decision": "approve"}, headers={"X-Knowledge-CSRF": "correct-token"},
        )
        assert allowed.status_code == 404
        assert client.post(archive_path, json=archive_body,
                           headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 404
        assert client.post(publication_path, json={
            "expected_snapshot_id": "not-yet-discovered", "expected_snapshot_version": 1,
            "rationale": "The source owner approves this metadata for agent use.",
        }, headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 409
        queued = client.post(
            f"/api/knowledge/connections/{connection_id}/refresh",
            headers={"X-Knowledge-CSRF": "correct-token"},
        )
        assert queued.status_code == 202
        job_id = queued.json()["job"]["id"]
        assert client.get(f"/api/knowledge/jobs/{job_id}").json()["job"]["status"] == "queued"
        client.cookies.set("knowledge_session", _cookie(settings, subject="different-user"))
        assert client.get(f"/api/knowledge/jobs/{job_id}").status_code == 404
        assert client.post(f"/api/knowledge/jobs/{job_id}/cancel",
                           headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 403
        client.cookies.set("knowledge_session", _cookie(
            settings, groups=("knowledge-users", "knowledge-stewards"),
        ))
        assert client.post(f"/api/knowledge/jobs/{job_id}/cancel",
                           headers={"X-Knowledge-CSRF": "correct-token"}).json()["job"]["status"] == "cancelled"
    store.close()


def test_oidc_browser_and_agent_share_source_grant_boundary(monkeypatch, tmp_path):
    settings = _settings(monkeypatch)
    monkeypatch.setenv("KNOWLEDGE_AGENT_SOURCE_MANAGEMENT", "1")
    monkeypatch.setenv("KNOWLEDGE_AGENT_ALLOWED_KINDS", "local_files")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(
        OidcAgentBoundary, "verify",
        lambda _self, token: {"sub": token, "groups": ["knowledge-users"], "scope": "knowledge.agent"},
    )
    app = create_platform(store=store)
    with TestClient(app, base_url=settings.public_url) as client:
        client.cookies.set("knowledge_session", _cookie(
            settings, subject="owner", groups=("knowledge-users", "knowledge-stewards"),
        ))
        created = client.post("/api/knowledge/connections", json={
            "name": "Private source", "kind": "local_files", "config": {},
        }, headers={"X-Knowledge-CSRF": "correct-token"})
        assert created.status_code == 201
        source_id = created.json()["connection"]["id"]
        queued = client.post(f"/api/knowledge/connections/{source_id}/refresh",
                             headers={"X-Knowledge-CSRF": "correct-token"})
        assert queued.status_code == 202
        job_id = queued.json()["job"]["id"]
        client.cookies.set("knowledge_session", _cookie(settings, subject="viewer"))
        assert client.get("/api/knowledge/connections").json()["connections"] == []
        assert client.get(f"/api/knowledge/connections/{source_id}").status_code == 404
        headers = {"Authorization": "Bearer agent-other"}
        assert client.get("/agent/api/sources", headers=headers).json()["connections"] == []
        assert client.get(f"/agent/api/sources/{source_id}/status", headers=headers).status_code == 404
        assert client.get(f"/agent/api/jobs/{job_id}", headers=headers).status_code == 404
        assert client.post("/agent/api/sources", headers=headers, json={
            "name": "Denied", "kind": "local_files", "config": {},
        }).status_code == 403
        mcp_headers = headers | {"Accept": "application/json, text/event-stream",
                                 "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
        mcp_call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_workspace", "arguments": {}}}
        denied_mcp = client.post("/agent/mcp/", headers=mcp_headers, json=mcp_call)
        assert denied_mcp.status_code == 200
        assert source_id not in denied_mcp.text
        job_call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "get_refresh_job", "arguments": {"job_id": job_id}}}
        assert "not_found" in client.post("/agent/mcp/", headers=mcp_headers, json=job_call).text
        assert client.post("https://untrusted.example.test/agent/mcp/",
                           headers=mcp_headers, json=mcp_call).status_code == 421
        store.access_enforced = False  # Offline operator provisioning in this test fixture.
        store.grant_source_access("demo", source_id, principal_type="user", principal_id="agent-other", role="read")
        store.access_enforced = True
        assert len(client.get("/agent/api/sources", headers=headers).json()["connections"]) == 1
        assert client.get(f"/agent/api/sources/{source_id}/status", headers=headers).status_code == 200
        assert client.get(f"/agent/api/jobs/{job_id}", headers=headers).json()["job"]["status"] == "queued"
        granted_mcp = client.post("/agent/mcp/", headers=mcp_headers, json=mcp_call)
        assert granted_mcp.status_code == 200
        assert source_id in granted_mcp.text
        assert job_id in client.post("/agent/mcp/", headers=mcp_headers, json=job_call).text
        store.access_enforced = False
        store.revoke_source_access("demo", source_id, principal_type="user", principal_id="agent-other")
        store.access_enforced = True
        assert client.get("/agent/api/sources", headers=headers).json()["connections"] == []
        assert client.get(f"/agent/api/jobs/{job_id}", headers=headers).status_code == 404
        revoked_mcp = client.post("/agent/mcp/", headers=mcp_headers, json=mcp_call)
        assert revoked_mcp.status_code == 200
        assert source_id not in revoked_mcp.text
    store.close()


def test_oidc_steward_publication_reaches_granted_agent_and_can_be_withdrawn(monkeypatch, tmp_path):
    settings = _settings(monkeypatch)
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Reviewed orders", config={"path": "."})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    evidence_id = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"])
    proposal = store.submit_contract_assertion(
        "demo", source["id"], assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": "One row per customer order."}, evidence_ids=[evidence_id],
        rationale="The source inventory supports this scoped description.", expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve",
                          rationale="The owner confirmed the order scope.")
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="owner", role="manage")
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="agent-reader", role="read")
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setattr(
        OidcAgentBoundary, "verify",
        lambda _self, token: {"sub": token, "groups": ["knowledge-users"], "scope": "knowledge.agent"},
    )
    app = create_platform(store=store)
    path = f"/api/knowledge/connections/{source['id']}/publication"
    with TestClient(app, base_url=settings.public_url) as client:
        client.cookies.set("knowledge_session", _cookie(settings, subject="owner"))
        assert client.get(path).json()["eligible"] is True
        reviewed = client.get(f"/api/knowledge/connections/{source['id']}").json()["snapshot"]
        body = {"expected_snapshot_id": reviewed["id"], "expected_snapshot_version": reviewed["version"],
                "rationale": "The owner accepts this reviewed source for agent use."}
        assert client.post(path, json=body, headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 403
        client.cookies.set("knowledge_session", _cookie(
            settings, subject="owner", groups=("knowledge-users", "knowledge-stewards"),
        ))
        assert client.post(path, json=body, headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 200
        headers = {"Authorization": "Bearer agent-reader"}
        agent_sources = client.get("/agent/api/sources", headers=headers).json()["connections"]
        assert agent_sources[0]["agent_ready"] is True
        assert client.post(f"/agent/api/sources/{source['id']}/publication", headers=headers,
                           json=body).status_code == 404
        assert client.post(f"{path}/withdraw", json={"reason": "The owner withdrew this version."},
                           headers={"X-Knowledge-CSRF": "correct-token"}).status_code == 200
        assert client.get("/agent/api/sources", headers=headers).json()["connections"][0]["agent_ready"] is False
    store.close()


def test_oidc_rejects_injected_unmanaged_store(monkeypatch):
    _settings(monkeypatch)
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    with pytest.raises(SchemaMigrationError, match="managed schema requires PostgreSQL"):
        create_platform(store=store)
    store.close()


def test_oidc_requires_operator_source_policy(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY")
    monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY_FILE", raising=False)
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    with pytest.raises(ValueError, match="requires KNOWLEDGE_SOURCE_POLICY"):
        create_platform(store=store)
    store.close()


def test_jwks_verification_does_not_block_loop_and_backs_off(monkeypatch):
    settings = _settings(monkeypatch)
    boundary = OidcAgentBoundary(lambda *_: None, settings)

    def slow_verify(_token):
        time.sleep(0.1)
        return {"sub": "agent"}

    monkeypatch.setattr(boundary, "verify", slow_verify)

    async def concurrent_tick():
        verification = asyncio.create_task(boundary.verify_async("token"))
        await asyncio.sleep(0.01)
        assert not verification.done()
        assert await verification == {"sub": "agent"}

    asyncio.run(concurrent_tick())
    calls = []

    def unavailable(token):
        calls.append(token)
        raise jwt.PyJWKClientConnectionError("test IdP outage")

    monkeypatch.setattr(boundary, "verify", unavailable)
    with pytest.raises(jwt.PyJWKClientConnectionError):
        asyncio.run(boundary.verify_async("token"))
    with pytest.raises(jwt.PyJWKClientConnectionError):
        asyncio.run(boundary.verify_async("token"))
    assert calls == ["token"]


def test_oidc_readiness_bounds_schema_checks_and_detects_drift(monkeypatch):
    settings = _settings(monkeypatch)
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    clock = [100.0]
    checks = []
    monkeypatch.setattr("knowledge_layer.platform._monotonic", lambda: clock[0])
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: checks.append("ok"))
    app = create_platform(store=store)
    with TestClient(app, base_url=settings.public_url) as client:
        assert client.get("/api/ready").status_code == 200
        assert client.get("/api/ready").status_code == 200
        assert checks == ["ok"]  # startup check only, despite repeated public probes

        def drift(_store):
            checks.append("drift")
            raise SchemaMigrationError("schema drift")

        monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", drift)
        clock[0] = 131.0
        assert client.get("/api/ready").status_code == 503
        assert client.get("/api/ready").status_code == 503
        assert checks == ["ok", "drift"]
    store.close()


def test_oidc_mode_fails_closed_without_external_postgres(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    with pytest.raises(ValueError, match="customer-managed PostgreSQL"):
        create_platform()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    with pytest.raises(ValueError, match="customer-managed PostgreSQL"):
        create_platform()
    monkeypatch.setenv("KNOWLEDGE_OIDC_STEWARD_GROUP", "knowledge-users")
    with pytest.raises(ValueError, match="steward group must differ"):
        OidcSettings()


def test_oidc_agent_checks_signature_audience_issuer_and_scope(monkeypatch):
    settings = _settings(monkeypatch)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    boundary = OidcAgentBoundary(lambda *_: None, settings)
    boundary.jwks.get_signing_key_from_jwt = lambda _token: SimpleNamespace(key=key.public_key())
    now = int(time.time())
    base = {"iss": settings.issuer, "aud": settings.audience, "sub": "service-agent",
            "exp": now + 300, "scope": "knowledge.agent"}

    def sign(claims):
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})

    assert boundary.verify(sign(base))["sub"] == "service-agent"
    for changed in ({"aud": "different"}, {"iss": "https://attacker.test"},
                    {"scope": "knowledge.read"}, {"exp": now - 1}):
        try:
            boundary.verify(sign(base | changed))
        except jwt.PyJWTError:
            pass
        else:
            raise AssertionError(f"Invalid agent token accepted: {changed}")
