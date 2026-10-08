"""Signed Google IAP identity never replaces source grants, role policy or CSRF."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from knowledge_layer.iap import (
    IAP_ISSUER,
    IAP_JWKS_URI,
    IapIdentityPolicy,
    IapSettings,
    IapVerifier,
)
from knowledge_layer.migrate import SchemaMigrationError
from knowledge_layer.platform import create_platform
from knowledge_layer.store import KnowledgeStore

IDENTITIES = {
    "version": 1,
    "allowed_subjects": ["accounts.google.com:owner", "accounts.google.com:reader"],
    "allowed_emails": ["bootstrap@example.test"],
    "steward_subjects": ["accounts.google.com:owner"], "steward_emails": [],
    "agent_subjects": ["accounts.google.com:agent"], "agent_emails": [],
}


@pytest.fixture
def iap_case(monkeypatch, tmp_path):
    policy_file = tmp_path / "identities.json"
    policy_file.write_text(json.dumps(IDENTITIES))
    policy_file.chmod(0o600)
    values = {
        "KNOWLEDGE_AUTH_MODE": "iap",
        "KNOWLEDGE_ENABLED_KINDS": "local_files",
        "KNOWLEDGE_IAP_AUDIENCE": "/projects/123456789/locations/us-central1/services/knowledge",
        "KNOWLEDGE_IAP_SESSION_KEY": "a-test-only-long-session-signing-key-for-iap",
        "KNOWLEDGE_IAP_IDENTITIES_FILE": str(policy_file),
        "KNOWLEDGE_OIDC_ALLOWED_GROUP": "knowledge-users",
        "KNOWLEDGE_OIDC_STEWARD_GROUP": "knowledge-stewards",
        "KNOWLEDGE_PUBLIC_URL": "https://knowledge.example.test",
        "KNOWLEDGE_SOURCE_POLICY": json.dumps({"version": 1, "sources": [{
            "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
            "allowed_subjects": ["accounts.google.com:owner"], "allowed_groups": [],
        }]}),
    }
    monkeypatch.delenv("KNOWLEDGE_IAP_IDENTITIES", raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = IapSettings()
    key = ec.generate_private_key(ec.SECP256R1())
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())) | {"kid": "google-key", "alg": "ES256", "use": "sig"}
    fetches = []
    real_client = httpx.Client

    def jwks(request):
        fetches.append(str(request.url))
        assert str(request.url) == IAP_JWKS_URI
        return httpx.Response(200, json={"keys": [public]})

    monkeypatch.setattr("knowledge_layer.iap.httpx.Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(jwks)))

    def token(subject="accounts.google.com:owner", **changes):
        now = int(time.time())
        claims = {"iss": IAP_ISSUER, "aud": settings.audience, "iat": now - 1,
                  "exp": now + 300, "sub": subject, "email": "owner@example.test"} | changes
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "google-key"})

    return {"settings": settings, "key": key, "token": token,
            "policy_file": policy_file, "fetches": fetches,
            "client_type": real_client, "public_key": public}


def test_slow_drip_jwks_refuses_with_total_deadline_and_does_not_cache(iap_case, monkeypatch):
    from types import SimpleNamespace

    from knowledge_layer import iap

    clock = [10.0]
    raw = json.dumps({"keys": [iap_case["public_key"]]}).encode()

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for value in raw:
                clock[0] += 2.9  # Every read remains below the per-read timeout.
                yield bytes([value])

    monkeypatch.setattr(iap, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(iap.httpx, "Client", lambda **kwargs: iap_case["client_type"](
        **kwargs, transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=SlowStream()))))
    verifier = IapVerifier(iap_case["settings"])
    with pytest.raises(jwt.InvalidTokenError, match="unavailable"):
        verifier.verify(iap_case["token"]())
    assert clock[0] == pytest.approx(15.8)
    assert verifier._keys == {}
    assert verifier._keys_lock.acquire(blocking=False)
    verifier._keys_lock.release()


def test_signing_key_lock_has_a_finite_acquisition_budget(iap_case):
    verifier = IapVerifier(iap_case["settings"])

    class BusyLock:
        def acquire(self, *, timeout):
            assert timeout == 3
            return False

    verifier._keys_lock = BusyLock()
    with pytest.raises(jwt.InvalidTokenError, match="busy"):
        verifier.verify(iap_case["token"]())


def test_assertions_verify_fixed_google_keys_signature_and_bounded_lifetime(iap_case):
    verifier = IapVerifier(iap_case["settings"])
    sign = iap_case["token"]
    assert verifier.verify(sign())["sub"] == "accounts.google.com:owner"
    assert verifier.verify(sign())["sub"] == "accounts.google.com:owner"
    assert iap_case["fetches"] == [IAP_JWKS_URI]
    now = int(time.time())
    for changes in ({"iss": "https://attacker.example.test"}, {"aud": "wrong"},
                    {"aud": [iap_case["settings"].audience]}, {"exp": now - 31},
                    {"iat": now + 31}, {"iat": now - 1, "exp": now + 660},
                    {"iat": now - 1, "exp": now - 2}, {"iat": True}, {"exp": str(now + 300)},
                    {"sub": ""}):
        with pytest.raises((jwt.PyJWTError, ValueError)):
            verifier.verify(sign(**changes))
    foreign = ec.generate_private_key(ec.SECP256R1())
    decoded = jwt.decode(sign(), options={"verify_signature": False})
    with pytest.raises(jwt.InvalidSignatureError):
        verifier.verify(jwt.encode(decoded, foreign, algorithm="ES256", headers={"kid": "google-key"}))
    with pytest.raises(jwt.InvalidAlgorithmError):
        verifier.verify(jwt.encode(decoded, "test-only-secret-with-thirty-two-bytes", algorithm="HS256", headers={"kid": "google-key"}))
    decoded.pop("iat")
    with pytest.raises(jwt.MissingRequiredClaimError):
        verifier.verify(jwt.encode(decoded, iap_case["key"], algorithm="ES256", headers={"kid": "google-key"}))


def test_iap_browser_requires_assertion_signed_csrf_origin_and_explicit_steward(iap_case, monkeypatch, tmp_path):
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    (tmp_path / "index.html").write_text("<html>Private application</html>")
    monkeypatch.setenv("KNOWLEDGE_WEB_DIST", str(tmp_path))
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    app = create_platform(store=store, agent_token="must-not-bypass-iap")
    body = {"kind": "local_files", "name": "Private source", "config": {}}
    owner = {"X-Goog-IAP-JWT-Assertion": iap_case["token"]()}
    reader = {"X-Goog-IAP-JWT-Assertion": iap_case["token"]("accounts.google.com:reader")}
    try:
        with TestClient(app, base_url=iap_case["settings"].public_url) as client:
            for path in ("/", "/auth/me", "/api/knowledge/connections", "/agent/api/sources"):
                assert client.get(path).status_code == 401
                assert client.get(path, headers={"X-Goog-Authenticated-User-Id": "owner", "Authorization": "Bearer must-not-bypass-iap"}).status_code == 401
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/ready").status_code == 200
            assert client.get("/", headers=owner).status_code == 200
            assert client.get("/api/knowledge/connections", headers=[
                ("X-Goog-IAP-JWT-Assertion", iap_case["token"]()),
                ("x-goog-iap-jwt-assertion", iap_case["token"]()),
            ]).status_code == 401
            assert client.get("/auth/me", headers={"X-Goog-IAP-JWT-Assertion": "x" * 16385}).status_code == 401
            me = client.get("/auth/me", headers=owner)
            assert me.status_code == 200 and me.json()["steward"] is True
            assert "secure" in me.headers["set-cookie"].lower() and "httponly" in me.headers["set-cookie"].lower()
            csrf = me.json()["csrf_token"]
            writes = owner | {"X-Knowledge-CSRF": csrf, "Origin": iap_case["settings"].public_url}
            assert client.post("/api/knowledge/connections", headers=owner, json=body).status_code == 403
            assert client.post("/api/knowledge/connections", headers=owner | {"X-Knowledge-CSRF": csrf}, json=body).status_code == 403
            assert client.post("/api/knowledge/connections", headers=writes | {"Origin": "https://attacker.test"}, json=body).status_code == 403
            assert client.post("/api/knowledge/connections", headers=reader | {"X-Knowledge-CSRF": csrf, "Origin": iap_case["settings"].public_url}, json=body).status_code == 403
            created = client.post("/api/knowledge/connections", headers=writes, json=body)
            assert created.status_code == 201, created.text
            source = created.json()["connection"]["id"]
            assert client.get(f"/api/knowledge/connections/{source}", headers=reader).status_code == 404
            queued = client.post(f"/api/knowledge/connections/{source}/refresh", headers=writes)
            assert queued.status_code == 202
            with store.engine.connect() as connection:
                row = connection.execute(store.refresh_jobs.select()).mappings().one()
                assert row["requested_by"] == "accounts.google.com:owner"
            reader_me = client.get("/auth/me", headers=reader)
            assert reader_me.json()["steward"] is False
            assert client.post("/auth/logout", headers=writes).status_code == 403  # identity changed
            current = client.get("/auth/me", headers=owner).json()["csrf_token"]
            payload, signature = client.cookies.get("knowledge_session").rsplit(".", 1)
            # Change signed bytes, not the unused padding bits of the final base64 character.
            changed = "a" if signature[0] != "a" else "b"
            tampered = payload + "." + changed + signature[1:]
            client.cookies.clear()
            client.cookies.set("knowledge_session", tampered)
            assert client.post("/auth/logout", headers=owner | {"X-Knowledge-CSRF": current, "Origin": iap_case["settings"].public_url}).status_code == 403
    finally:
        store.close()


def test_iap_agent_has_separate_allowlist_and_source_grants(iap_case, monkeypatch):
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    monkeypatch.setenv("KNOWLEDGE_AGENT_SOURCE_MANAGEMENT", "1")
    monkeypatch.setenv("KNOWLEDGE_AGENT_ALLOWED_KINDS", "local_files")
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    source = store.create_connection("demo", kind="local_files", name="Owner only", config={})
    app = create_platform(store=store, agent_token="no-bypass")
    agent = {"X-Goog-IAP-JWT-Assertion": iap_case["token"]("accounts.google.com:agent", groups=["knowledge-stewards"], scope="knowledge.sources.manage")}
    try:
        with TestClient(app, base_url=iap_case["settings"].public_url) as client:
            assert client.get("/agent/api/sources", headers={"X-Goog-IAP-JWT-Assertion": iap_case["token"]()}).status_code == 403
            assert client.get("/api/knowledge/connections", headers=agent).status_code == 403
            assert client.get("/agent/api/sources", headers=agent).json()["connections"] == []
            assert client.get(f"/agent/api/sources/{source['id']}/status", headers=agent).status_code == 404
            assert client.post("/agent/api/sources", headers=agent, json={"kind": "local_files", "name": "Denied", "config": {}}).status_code == 403
            store.access_enforced = False
            store.grant_source_access("demo", source["id"], principal_type="user", principal_id="accounts.google.com:agent", role="read")
            store.access_enforced = True
            assert len(client.get("/agent/api/sources", headers=agent).json()["connections"]) == 1
            mcp = client.post("/agent/mcp/", headers=agent | {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_workspace", "arguments": {}}})
            assert mcp.status_code == 200 and source["id"] in mcp.text
            policy = dict(IDENTITIES) | {"agent_subjects": []}
            iap_case["policy_file"].write_text(json.dumps(policy))
            assert client.get("/agent/api/sources", headers=agent).status_code == 403
    finally:
        store.close()


def test_signed_email_bootstraps_canonical_subject_without_unsigned_alias(iap_case, monkeypatch):
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        with TestClient(create_platform(store=store), base_url=iap_case["settings"].public_url) as client:
            assert client.get("/auth/me", headers={"X-Goog-IAP-JWT-Assertion": iap_case["token"]("new-subject", email="bootstrap@example.test")}).json()["subject"] == "new-subject"
            assert client.get("/auth/me", headers={"X-Goog-IAP-JWT-Assertion": iap_case["token"]("new-subject", email="outsider@example.test"), "X-Goog-Authenticated-User-Email": "accounts.google.com:bootstrap@example.test"}).status_code == 403
            iap_case["policy_file"].write_text("invalid policy")
            assert client.get("/auth/me", headers={"X-Goog-IAP-JWT-Assertion": iap_case["token"]()}).status_code == 401
    finally:
        store.close()


def test_iap_managed_schema_and_source_policy_are_required(iap_case, monkeypatch):
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        with pytest.raises(SchemaMigrationError, match="managed schema requires PostgreSQL"):
            create_platform(store=store)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
        with pytest.raises(ValueError, match="customer-managed PostgreSQL"):
            create_platform()
        monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY")
        monkeypatch.delenv("KNOWLEDGE_SOURCE_POLICY_FILE", raising=False)
        with pytest.raises(ValueError, match="requires KNOWLEDGE_SOURCE_POLICY"):
            create_platform(store=store)
    finally:
        store.close()


def test_iap_identity_policy_bounds_duplicates_unknown_fields_and_file_permissions(iap_case, monkeypatch):
    for change in ({"extra": True}, {"agent_subjects": ["a"] * 2}, {"allowed_subjects": [], "allowed_emails": []}, {"allowed_emails": ["*"]}, {"version": True}, {"allowed_subjects": ["a"] * 257}):
        iap_case["policy_file"].write_text(json.dumps(dict(IDENTITIES) | change))
        with pytest.raises(ValueError):
            IapIdentityPolicy.from_environment()
    iap_case["policy_file"].write_text(json.dumps(IDENTITIES))
    iap_case["policy_file"].chmod(0o644)
    with pytest.raises(ValueError, match="owner"):
        IapIdentityPolicy.from_environment()
    iap_case["policy_file"].chmod(0o600)
    monkeypatch.setenv("KNOWLEDGE_IAP_AUDIENCE", "/projects/123/global/backendServices/456")
    with pytest.raises(ValueError, match="direct Cloud Run"):
        IapSettings()


def test_iap_without_discovered_origin_refuses_browser_writes(iap_case, monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_PUBLIC_URL")
    monkeypatch.setattr("knowledge_layer.migrate.ensure_current_schema", lambda _store: None)
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        with TestClient(create_platform(store=store), base_url="https://discovered.example.test") as client:
            headers = {"X-Goog-IAP-JWT-Assertion": iap_case["token"]()}
            csrf = client.get("/auth/me", headers=headers).json()["csrf_token"]
            response = client.post("/auth/logout", headers=headers | {
                "X-Knowledge-CSRF": csrf, "Origin": "https://discovered.example.test"})
            assert response.status_code == 503
    finally:
        store.close()


def test_iap_worker_retains_managed_policy_and_migration_boundary(iap_case, monkeypatch):
    from knowledge_layer import worker
    from knowledge_layer.source_policy import SourcePolicy

    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    checked = []
    monkeypatch.setattr(worker, "ensure_current_schema", lambda store: checked.append(store))
    store = worker._store()
    try:
        assert checked == [store]
        assert isinstance(store.source_policy, SourcePolicy)
        assert isinstance(store.source_policy_loader(), SourcePolicy)
        # A worker remains a trusted registry coordinator; request identities
        # come from persisted jobs and direct grants, never proxy user headers.
        assert store.access_enforced is False
    finally:
        store.close()
