"""A self-hosted owner claim never grants agent human-review authority."""
from __future__ import annotations

import json
import secrets
import stat

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.local_auth import COOKIE, LocalSettings
from knowledge_layer.origin import normalize_origin
from knowledge_layer.platform import create_platform
from knowledge_layer.refresh_authorization import require_refresh_requester
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeStore


@pytest.fixture(params=["memory", "file"])
def local_case(monkeypatch, tmp_path, request):
    directory = tmp_path / "auth"
    directory.mkdir(mode=0o700)
    origin = "http://127.0.0.1:4499"
    bootstrap, agent, password = (secrets.token_urlsafe(32) for _ in range(3))
    for key in ("KNOWLEDGE_SOURCE_POLICY", "KNOWLEDGE_SOURCE_POLICY_FILE",
                "KNOWLEDGE_LOCAL_SESSION_KEY_FILE", "KNOWLEDGE_LOCAL_BOOTSTRAP_TOKEN_FILE",
                "KNOWLEDGE_AGENT_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        "KNOWLEDGE_AUTH_MODE": "local", "KNOWLEDGE_PUBLIC_URL": origin,
        "KNOWLEDGE_LOCAL_AUTH_FILE": str(directory / "owner.json"),
        "KNOWLEDGE_LOCAL_SESSION_KEY": secrets.token_urlsafe(32),
        "KNOWLEDGE_LOCAL_BOOTSTRAP_TOKEN": bootstrap, "KNOWLEDGE_AGENT_TOKEN": agent,
        "KNOWLEDGE_ENABLED_KINDS": "local_files", "KNOWLEDGE_AGENT_SOURCE_MANAGEMENT": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    database_url = "sqlite:///:memory:" if request.param == "memory" else f"sqlite:///{tmp_path / 'registry.db'}"
    store = KnowledgeStore(database_url=database_url, local_files_root=tmp_path)
    app = create_platform(store=store)
    body = {"name": "Private owner", "password": password, "bootstrap_token": bootstrap}
    with TestClient(app, base_url=origin) as client:
        yield {"client": client, "store": store, "app": app, "origin": origin,
               "body": body, "path": directory / "owner.json", "agent": agent}
    store.close()


def claim(case):
    response = case["client"].post("/auth/setup", json=case["body"], headers={"Origin": case["origin"]})
    assert response.status_code == 200, response.text
    return {"Origin": case["origin"], "X-Knowledge-CSRF": response.json()["csrf_token"]}


def test_claim_is_operator_bound_atomic_and_secret_safe(local_case):
    case = local_case
    client = case["client"]
    assert client.get("/auth/status").json()["bootstrap_required"] is True
    assert client.get("/auth/me").status_code == 401
    assert client.get("/api/knowledge/connections").status_code == 401
    assert client.get("/api/setup").status_code == 401
    assert client.post("/auth/setup", json=case["body"]).status_code == 403
    assert client.post("/auth/setup", json=case["body"], headers={"Origin": "https://attacker.test"}).status_code == 403
    wrong = case["body"] | {"bootstrap_token": "untrusted"}
    assert client.post("/auth/setup", json=wrong, headers={"Origin": case["origin"]}).status_code == 403
    headers = claim(case)
    record = json.loads(case["path"].read_text())
    assert case["body"]["password"] not in case["path"].read_text()
    assert case["body"]["bootstrap_token"] not in case["path"].read_text()
    assert stat.S_IMODE(case["path"].stat().st_mode) == 0o600
    assert len(record["hash"]) == 64 and record["hash"] != case["body"]["password"]
    assert client.get("/auth/status").json()["authenticated"] is True
    cookie = client.cookies.get(COOKIE)
    assert cookie
    response = client.post("/auth/setup", json=case["body"], headers=headers)
    assert response.status_code == 409 and json.loads(case["path"].read_text()) == record


def test_cookie_csrf_origin_logout_and_login_replay(local_case):
    case = local_case
    client = case["client"]
    headers = claim(case)
    cookie = client.cookies.get(COOKIE)
    assert client.post("/auth/logout", headers={"Origin": case["origin"]}).status_code == 403
    assert client.post("/auth/logout", headers=headers | {"Origin": "https://attacker.test"}).status_code == 403
    assert client.post("/auth/logout", headers=headers).status_code == 200
    client.cookies.set(COOKIE, cookie)
    assert client.get("/auth/me").status_code == 401  # Signed but revoked on the server.
    client.cookies.clear()
    response = client.post("/auth/login", json={"name": "Private owner", "password": case["body"]["password"]},
                           headers={"Origin": case["origin"]})
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert client.get("/auth/me").json()["steward"] is True


def test_owner_source_shares_read_only_agent_and_audits_exact_actor(local_case):
    case = local_case
    headers = claim(case)
    response = case["client"].post("/api/knowledge/connections", json={
        "kind": "local_files", "name": "Private files", "config": {"path": ".", "include": "**/*"},
    }, headers=headers)
    assert response.status_code == 201, response.text
    source = response.json()["connection"]["id"]
    assert case["client"].get("/agent/api/sources").status_code == 401
    bearer = {"Authorization": "Bearer " + case["agent"]}
    assert len(case["client"].get("/agent/api/sources", headers=bearer).json()["connections"]) == 1
    assert case["client"].post("/agent/api/sources", json={"kind": "local_files", "name": "Unapproved", "config": {}},
                               headers=bearer).status_code == 405
    result = case["client"].post(f"/api/knowledge/connections/{source}/refresh", headers=headers)
    assert result.status_code == 202
    with case["store"].engine.connect() as conn:
        grants = conn.execute(select(case["store"].source_grants)).mappings().all()
        assert {(row["principal_id"], row["role"]) for row in grants} == {("local-owner", "manage"), ("local-agent", "read")}
        job = conn.execute(select(case["store"].refresh_jobs)).mappings().one()
        assert job["requested_by"] == "local-owner"
        require_refresh_requester(case["store"], conn, job)
        actors = conn.execute(select(case["store"].audit_events.c.actor)).scalars().all()
        assert len(actors) >= 4 and set(actors) == {"local-owner"}


def test_boundaries_refuse_host_changes_invalid_records_and_secret_echo(local_case):
    case = local_case
    assert case["client"].get("/auth/status", headers={"Host": "attacker.test"}).status_code == 403
    assert case["client"].get("/api/ready", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    bad = case["body"] | {"password": "short"}
    response = case["client"].post("/auth/setup", json=bad, headers={"Origin": case["origin"]})
    assert response.status_code == 422 and '"short"' not in response.text
    claim(case)
    record = case["path"].read_text()
    case["path"].chmod(0o644)
    assert case["client"].get("/auth/status").status_code == 503
    assert case["client"].get("/agent/api/sources", headers={"Authorization": "Bearer " + case["agent"]}).status_code == 503
    case["path"].chmod(0o600)
    case["path"].write_text(record.replace('"version": 1', '"version": 1, "version": 1'))
    assert case["client"].get("/auth/status").status_code == 503


def test_setup_configs_are_references_and_do_not_claim_a_connected_client(local_case):
    case = local_case
    claim(case)
    response = case["client"].get("/api/setup")
    assert response.status_code == 200
    body = response.json()
    assert case["agent"] not in response.text and case["body"]["password"] not in response.text
    assert "${env:KNOWLEDGE_AGENT_TOKEN}" in body["agent"]["clients"]["cursor"]
    assert body["agent"]["endpoint"] == case["origin"] + "/agent/mcp/"
    assert next(item for item in body["checks"] if item["id"] == "agent")["status"] == "action"
    assert body["next_step"] == "agent"


def test_agent_credential_cannot_accept_or_publish(local_case):
    case = local_case
    headers = claim(case)
    response = case["client"].post("/api/knowledge/connections", json={
        "kind": "local_files", "name": "Scoped files", "config": {"path": "."},
    }, headers=headers)
    source = response.json()["connection"]["id"]
    case["client"].cookies.clear()
    bearer = {"Authorization": "Bearer " + case["agent"]}
    body = {"proposal_ids": ["draft"], "expected_snapshot_id": "snapshot",
            "expected_snapshot_version": 1, "expected_review_digest": "a" * 64,
            "rationale": "This agent must never accept its own draft.", "publish": True}
    for path in (f"/api/knowledge/connections/{source}/review-batch/accept",
                 f"/api/knowledge/connections/{source}/publication"):
        assert case["client"].post(path, json=body, headers=bearer).status_code == 401
    build = case["client"].post("/agent/api/builds", json={"connection_ids": [source]}, headers=bearer)
    assert build.status_code == 200
    with case["store"].engine.connect() as conn:
        assert conn.execute(select(case["store"].builds.c.created_by)).scalar_one() == "local-agent"
    assert case["client"].post(f"/agent/api/sources/{source}/review-batch/accept",
                               json=body, headers=bearer).status_code == 404


def test_owner_acceptance_persists_agent_draft_and_publication(local_case):
    case = local_case
    headers = claim(case)
    response = case["client"].post("/api/knowledge/connections", json={
        "kind": "local_files", "name": "Observed orders", "config": {"path": ".", "include": "orders.csv"},
    }, headers=headers)
    source = response.json()["connection"]["id"]
    owner = Principal("local-owner", frozenset({"knowledge-users", "knowledge-stewards"}), frozenset())
    with principal_scope(owner):
        snapshot = case["store"].refresh_connection("demo", source)["snapshot"]
    asset = snapshot["assets"][0]
    evidence = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"])
    bearer = {"Authorization": "Bearer " + case["agent"]}
    submitted = case["client"].post("/agent/api/contract-assertions", headers=bearer, json={
        "connection_id": source, "assertion_kind": "dataset_description", "asset_id": asset["id"],
        "payload": {"description": "Observed order records."}, "evidence_ids": [evidence],
        "rationale": "The selected file contains an observed order record.", "expected_snapshot_id": snapshot["id"],
    })
    assert submitted.status_code == 202, submitted.text
    arguments = {"proposal_ids": [submitted.json()["proposal"]["id"]], "expected_snapshot_id": snapshot["id"],
                 "expected_snapshot_version": snapshot["version"]}
    path = f"/api/knowledge/connections/{source}/review-batch"
    preview = case["client"].post(path + "/preview", json=arguments, headers=headers)
    assert preview.status_code == 200, preview.text
    accepted = case["client"].post(path + "/accept", headers=headers, json={**arguments,
        "expected_review_digest": preview.json()["review_digest"], "publish": True,
        "rationale": "I reviewed the exact observed order description and its evidence.",
    })
    assert accepted.status_code == 200 and accepted.json()["status"] == "published", accepted.text
    with principal_scope(owner):
        current = case["store"].get_snapshot("demo", source)
    assert next(item for item in current["proposals"] if item["id"] == arguments["proposal_ids"][0])["status"] == "approved"
    assert current["agent_ready"] is True
    with case["store"].engine.connect() as conn:
        assert conn.execute(select(case["store"].decisions)).all()
    assert case["client"].get(f"/agent/api/sources/{source}/contract", headers=bearer).status_code == 200


def test_explicit_empty_or_unreadable_policy_never_falls_back_to_owner_policy(local_case, monkeypatch):
    case = local_case
    claim(case)
    policy = case["path"].parent / "source-policy.json"
    policy.write_text('{"version":1,"sources":[]}', encoding="utf-8")
    policy.chmod(0o600)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_POLICY_FILE", str(policy))
    assert case["store"]._current_source_policy().approvals == ()
    for action in (lambda: policy.write_text("", encoding="utf-8"), lambda: policy.unlink()):
        action()
        with pytest.raises(KnowledgeAccessDenied, match="policy is unavailable"):
            case["store"]._current_source_policy()
    monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY_FILE")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_POLICY", "")
    with pytest.raises(KnowledgeAccessDenied, match="policy is unavailable"):
        case["store"]._current_source_policy()


@pytest.mark.parametrize("value,expected", [
    ("https://EXAMPLE.com:443/", "https://example.com"),
    ("http://LOCALHOST:80", "http://localhost"),
    ("http://[::1]:80", "http://[::1]"),
    ("https://example.com:8443", "https://example.com:8443"),
])
def test_public_origin_matches_browser_canonical_form(value, expected):
    assert normalize_origin(value) == expected


@pytest.mark.parametrize("value", ["https://foo\\evil.test", "https://foo$.test",
                                   "https://[fe80::1%eth0]", "https://-bad.test", "https://a..test",
                                   "https://evil.test\u0000", "https://evil.test/path"])
def test_public_origin_refuses_ambiguous_hostnames(value):
    with pytest.raises(ValueError):
        normalize_origin(value)


def test_https_default_port_host_and_origin_are_accepted(local_case, monkeypatch):
    case = local_case
    monkeypatch.setenv("KNOWLEDGE_PUBLIC_URL", "https://EXAMPLE.test:443/")
    app = create_platform(store=case["store"])
    with TestClient(app, base_url="https://example.test") as client:
        assert client.get("/auth/status").status_code == 200
        response = client.post("/auth/setup", json=case["body"], headers={"Origin": "https://example.test"})
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()


def test_local_owner_mode_refuses_public_http_and_unprotected_parent(local_case, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_PUBLIC_URL", "http://public.example.test")
    with pytest.raises(ValueError, match="HTTPS"):
        LocalSettings()
    monkeypatch.setenv("KNOWLEDGE_PUBLIC_URL", local_case["origin"])
    local_case["path"].parent.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        LocalSettings()


def test_failed_password_work_is_rate_bounded(local_case):
    case = local_case
    for _ in range(12):
        result = case["client"].post("/auth/setup", json=case["body"] | {"bootstrap_token": "untrusted"},
                                   headers={"Origin": case["origin"]})
        assert result.status_code == 403
    assert case["client"].post("/auth/setup", json=case["body"], headers={"Origin": case["origin"]}).status_code == 429
