"""Run an optional, disposable OIDC integration drill against real Keycloak JWTs.

Requires Docker, OpenSSL, the development Python environment, and two free
loopback ports. Keycloak and PostgreSQL are stopped and removed on exit. No
customer identity provider, source account, or permanent test volume is used.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from knowledge_layer.migrate import run as migrate
from knowledge_layer.store import KnowledgeStore

ROOT = Path(__file__).resolve().parents[1]
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.7.5"
POSTGRES_IMAGE = "postgres:18.6-alpine"
PUBLIC_URL = "https://knowledge.localhost"
SERVICE_CLIENT = "knowledge-delegation-service"
DELEGATION_SCOPES = "knowledge.agent knowledge.sources.manage knowledge.autonomy"
MCP_ROUTES = ("/agent/mcp/", "/agent/advanced-mcp/")
OWNERSHIP_LABEL = "knowledge-layer.oidc-drill"
IMAGE_PULL_TIMEOUT_SECONDS = 180
LAUNCH_TIMEOUT_SECONDS = 45
SAFE_STAGES = frozenset({
    "bootstrap", "image_pull_postgres", "image_pull_keycloak", "fixture", "launch_postgres",
    "launch_keycloak", "readiness", "registry_checks", "cleanup", "cleanup_postgres", "cleanup_keycloak", "report_write",
})
SAFE_FAILURES = {
    "unclassified_error": "Disposable drill failed without safe driver details",
    "docker_operation_unsupported": "Disposable Docker operation is unsupported",
    "docker_unavailable": "Disposable Docker command could not be invoked",
    "image_pull_timeout": "Disposable image pull did not complete",
    "image_pull_failed": "Disposable image pull failed",
    "docker_launch_timeout": "Disposable Docker command did not complete",
    "docker_launch_failed": "Disposable Docker command failed",
    "readiness_timeout": "Disposable Keycloak or PostgreSQL did not become ready",
    "cleanup_list_failed": "Disposable container absence could not be verified",
    "cleanup_list_timeout": "Disposable container absence could not be verified",
    "cleanup_identity_mismatch": "Disposable container identity changed",
    "cleanup_inspect_failed": "Disposable container ownership could not be verified",
    "cleanup_inspect_timeout": "Disposable container ownership could not be verified",
    "cleanup_labels_invalid": "Disposable container ownership could not be verified",
    "cleanup_ownership_mismatch": "Disposable container ownership changed",
    "cleanup_remove_failed": "Disposable container removal failed",
    "cleanup_remove_timeout": "Disposable container removal did not complete",
    "cleanup_verify_failed": "Disposable container removal could not be verified",
    "cleanup_verify_timeout": "Disposable container removal could not be verified",
    "cleanup_remaining": "Disposable container removal could not be verified",
    "cleanup_failed": "Disposable resource cleanup could not be verified",
}


class DrillFailure(RuntimeError):
    """Only constant failure codes and phases may cross the output boundary."""

    def __init__(self, code: str, stage: str):
        if code not in SAFE_FAILURES or stage not in SAFE_STAGES:
            raise ValueError("Unsupported safe drill diagnostic")
        self.code, self.stage = code, stage
        self.previous_failure: DrillFailure | None = None
        super().__init__(SAFE_FAILURES[code])


def _safe_failure(exc: Exception, stage: str) -> dict[str, str | dict[str, str]]:
    code = exc.code if isinstance(exc, DrillFailure) and exc.code in SAFE_FAILURES else "unclassified_error"
    phase = exc.stage if isinstance(exc, DrillFailure) and exc.stage in SAFE_STAGES else stage
    result: dict[str, str | dict[str, str]] = {
        "result": "failed", "error_code": code, "stage": phase if phase in SAFE_STAGES else "bootstrap",
    }
    if isinstance(exc, DrillFailure) and exc.previous_failure is not None:
        previous = exc.previous_failure
        if previous.code in SAFE_FAILURES and previous.stage in SAFE_STAGES:
            result["previous_failure"] = {"error_code": previous.code, "stage": previous.stage}
    return result


class _LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and not self.action:
            self.action = values.get("action") or ""
        if tag == "input" and values.get("name"):
            self.fields[values["name"]] = values.get("value") or ""


def _docker(*args: str) -> str:
    if len(args) == 2 and args[0] == "pull" and args[1] in {POSTGRES_IMAGE, KEYCLOAK_IMAGE}:
        stage = "image_pull_postgres" if args[1] == POSTGRES_IMAGE else "image_pull_keycloak"
        timeout, failure, expired = IMAGE_PULL_TIMEOUT_SECONDS, "image_pull_failed", "image_pull_timeout"
    elif args and args[0] == "run" and (POSTGRES_IMAGE in args or KEYCLOAK_IMAGE in args):
        stage = "launch_postgres" if POSTGRES_IMAGE in args else "launch_keycloak"
        timeout, failure, expired = LAUNCH_TIMEOUT_SECONDS, "docker_launch_failed", "docker_launch_timeout"
    else:
        raise DrillFailure("docker_operation_unsupported", "bootstrap")
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DrillFailure(expired, stage) from None
    except OSError:
        raise DrillFailure("docker_unavailable", stage) from None
    if result.returncode:
        raise DrillFailure(failure, stage)
    return result.stdout.strip()


def _pull_known_images() -> None:
    # Fixed public images only. Capture, never print, Docker/registry diagnostics
    # and give cold downloads their own budget instead of spending launch time.
    for image in (POSTGRES_IMAGE, KEYCLOAK_IMAGE):
        _docker("pull", image)


def _cleanup_container(name: str) -> None:
    """Resolve only an intended exact name and verify nonce ownership first."""
    stage = ("cleanup_postgres" if name.startswith("knowledge-oidc-pg-") else
             "cleanup_keycloak" if name.startswith("knowledge-oidc-keycloak-") else "cleanup")
    operation = "cleanup_list"
    try:
        listed = subprocess.run(["docker", "container", "ls", "-a", "--filter", "name=^/" + name + "$",
                                 "--format", "{{.Names}}"], capture_output=True, text=True, check=False, timeout=5)
        if listed.returncode:
            raise DrillFailure("cleanup_list_failed", stage)
        if not listed.stdout.strip():
            return
        if listed.stdout.strip() != name:
            raise DrillFailure("cleanup_identity_mismatch", stage)
        operation = "cleanup_inspect"
        inspected = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", name],
                                   capture_output=True, text=True, check=False, timeout=5)
        if inspected.returncode:
            raise DrillFailure("cleanup_inspect_failed", stage)
        labels = json.loads(inspected.stdout)
        if not isinstance(labels, dict) or labels.get(OWNERSHIP_LABEL) != name:
            raise DrillFailure("cleanup_ownership_mismatch", stage)
        operation = "cleanup_remove"
        removed = subprocess.run(["docker", "rm", "--force", name],
                                 capture_output=True, text=True, check=False, timeout=15)
        if removed.returncode:
            raise DrillFailure("cleanup_remove_failed", stage)
        operation = "cleanup_verify"
        remaining = subprocess.run(["docker", "container", "ls", "-a", "--filter", "name=^/" + name + "$",
                                    "--format", "{{.Names}}"], capture_output=True, text=True, check=False, timeout=5)
        if remaining.returncode:
            raise DrillFailure("cleanup_verify_failed", stage)
        if remaining.stdout.strip():
            raise DrillFailure("cleanup_remaining", stage)
    except subprocess.TimeoutExpired:
        raise DrillFailure(operation + "_timeout", stage) from None
    except OSError:
        raise DrillFailure("cleanup_failed", stage) from None
    except ValueError:
        raise DrillFailure("cleanup_labels_invalid", stage) from None


def _cleanup_targets(names: list[str]) -> None:
    failed = None
    for name in reversed(names):
        try:
            _cleanup_container(name)
        except DrillFailure as exc:
            failed = failed or exc
        except RuntimeError:
            failed = failed or DrillFailure("cleanup_failed", "cleanup")
    if failed is not None:
        raise failed


def _secret(path: Path) -> str:
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


def _fixture(path: Path) -> dict[str, str]:
    values = {name: _secret(path / name) for name in (
        "web_secret", "other_secret", "owner_password", "reader_password",
        "admin_password", "pg_password", "session_key", "service_secret",
    )}
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(path / "tls.key"), "-out", str(path / "tls.crt"),
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (path / "tls.key").chmod(0o600)

    def client(client_id: str, secret_name: str, *, browser: bool, service: bool = False) -> dict:
        mappers = [{
            "name": "groups", "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper", "consentRequired": False,
            "config": {"full.path": "false", "id.token.claim": "true",
                       "access.token.claim": "true", "claim.name": "groups"},
        }]
        if browser or service:
            mappers.append({
                "name": "knowledge-audience", "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper", "consentRequired": False,
                "config": {"included.custom.audience": "knowledge-agent",
                           "id.token.claim": "false", "access.token.claim": "true"},
            })
        return {
            "clientId": client_id, "secret": values[secret_name], "publicClient": False,
            "standardFlowEnabled": browser, "directAccessGrantsEnabled": not service,
            "serviceAccountsEnabled": service,
            "redirectUris": [PUBLIC_URL + "/auth/callback"] if browser else [],
            "webOrigins": [PUBLIC_URL] if browser else [],
            "protocol": "openid-connect", "protocolMappers": mappers,
        }

    realm = {
        "realm": "knowledge-drill", "enabled": True, "sslRequired": "none",
        "verifyEmail": False, "accessTokenLifespan": 30,
        "groups": [{"name": "knowledge-users"}, {"name": "knowledge-stewards"}],
        "clients": [client("knowledge-web", "web_secret", browser=True),
                    client("knowledge-other", "other_secret", browser=False),
                    client(SERVICE_CLIENT, "service_secret", browser=False, service=True)],
        "users": [{
            "username": name, "email": f"{name}@example.invalid", "emailVerified": True,
            "firstName": name.title(), "lastName": "Drill", "enabled": True,
            "requiredActions": [], "credentials": [{
                "type": "password", "value": values[f"{name}_password"], "temporary": False,
            }], "groups": groups,
        } for name, groups in (("owner", ["/knowledge-users", "/knowledge-stewards"]),
                               ("reader", ["/knowledge-users"]))],
    }
    (path / "knowledge-drill-realm.json").write_text(json.dumps(realm), encoding="utf-8")
    (path / "knowledge-drill-realm.json").chmod(0o600)
    (path / "admin.env").write_text(
        "KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD="
        + values["admin_password"] + "\n", encoding="utf-8",
    )
    (path / "admin.env").chmod(0o600)
    (path / "pg.env").write_text(
        "POSTGRES_USER=knowledge_drill\nPOSTGRES_DB=knowledge_drill\nPOSTGRES_PASSWORD="
        + values["pg_password"] + "\n", encoding="utf-8",
    )
    (path / "pg.env").chmod(0o600)
    (path / "data").mkdir()
    (path / "data" / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    return values


def _wait_ready(issuer: str, cert: Path, pg_name: str, *, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            pg = subprocess.run([
                "docker", "exec", pg_name, "pg_isready", "-h", "127.0.0.1", "-U", "knowledge_drill",
                "-d", "knowledge_drill",
            ], capture_output=True, timeout=3, check=False)
            with httpx.Client(verify=str(cert), timeout=3) as client:
                idp = client.get(issuer + "/.well-known/openid-configuration")
            if pg.returncode == 0 and idp.status_code == 200 and idp.json()["issuer"] == issuer:
                return
        except (OSError, ValueError, httpx.HTTPError, subprocess.TimeoutExpired):
            pass
        time.sleep(1)
    raise DrillFailure("readiness_timeout", "readiness")


def _claims(token: str) -> dict:
    encoded = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def _keycloak_admin(http: httpx.Client, issuer: str, values: dict[str, str]) -> tuple[str, str]:
    base = issuer.split("/realms/")[0]
    response = http.post(base + "/realms/master/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": "admin-cli", "username": "admin",
        "password": values["admin_password"],
    })
    response.raise_for_status()
    headers = {"Authorization": "Bearer " + response.json()["access_token"]}
    admin = base + "/admin/realms/knowledge-drill"
    names = DELEGATION_SCOPES.split()
    for name in names:
        response = http.post(admin + "/client-scopes", json={
            "name": name, "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": "true"},
        }, headers=headers)
        response.raise_for_status()
    scopes = http.get(admin + "/client-scopes", headers=headers).json()
    scope_ids = [next(item["id"] for item in scopes if item["name"] == name) for name in names]
    for client_id in ("knowledge-web", "knowledge-other", SERVICE_CLIENT):
        clients = http.get(admin + "/clients", params={"clientId": client_id}, headers=headers).json()
        assert len(clients) == 1
        for scope_id in scope_ids:
            response = http.put(
                admin + f"/clients/{clients[0]['id']}/optional-client-scopes/{scope_id}", headers=headers,
            )
            response.raise_for_status()
    return admin, headers["Authorization"]


def _token(http: httpx.Client, issuer: str, values: dict[str, str], user: str,
           *, client_id: str = "knowledge-web", scope: str = "openid knowledge.agent") -> str:
    secret_name = "web_secret" if client_id == "knowledge-web" else "other_secret"
    response = http.post(issuer + "/protocol/openid-connect/token", data={
        "grant_type": "password", "client_id": client_id,
        "client_secret": values[secret_name], "username": user,
        "password": values[f"{user}_password"], "scope": scope,
    })
    response.raise_for_status()
    return response.json()["access_token"]


def _service_token(http: httpx.Client, issuer: str, values: dict[str, str], *,
                   scope: str = DELEGATION_SCOPES) -> str:
    response = http.post(issuer + "/protocol/openid-connect/token", data={
        "grant_type": "client_credentials", "client_id": SERVICE_CLIENT,
        "client_secret": values["service_secret"], "scope": scope,
    })
    response.raise_for_status()
    return response.json()["access_token"]


def _service_subject(http: httpx.Client, admin: str, authorization: str) -> str:
    headers = {"Authorization": authorization}
    response = http.get(admin + "/clients", params={"clientId": SERVICE_CLIENT}, headers=headers)
    response.raise_for_status()
    clients = response.json()
    assert len(clients) == 1 and clients[0]["clientId"] == SERVICE_CLIENT
    response = http.get(admin + f"/clients/{clients[0]['id']}/service-account-user", headers=headers)
    response.raise_for_status()
    account = response.json()
    assert account["username"] == "service-account-" + SERVICE_CLIENT
    return account["id"]


def _mcp_call(client: TestClient, token: str, route: str, name: str, arguments: dict) -> dict:
    response = client.post(route, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json", "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call", "Mcp-Name": name,
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": arguments, "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "disposable-delegation-harness", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }})
    assert response.status_code == 200
    return response.json()["result"]


def _browser_login(client: TestClient, http: httpx.Client, user: str,
                   values: dict[str, str]) -> httpx.Response:
    start = client.get("/auth/login")
    assert start.status_code == 302
    page = http.get(start.headers["location"])
    page.raise_for_status()
    form = _LoginForm()
    form.feed(page.text)
    assert form.action and "username" in form.fields and "password" in form.fields
    submitted = http.post(urljoin(str(page.url), form.action), data={
        **form.fields, "username": user, "password": values[f"{user}_password"],
    })
    assert submitted.status_code == 302
    callback = submitted.headers["location"]
    assert urlparse(callback).netloc == "knowledge.localhost"
    return client.get(callback)


def _assert_delegation_refused(browser: TestClient, token: str, connection_id: str,
                               request: dict, *, http_status: int) -> dict:
    response = browser.post(f"/agent/api/sources/{connection_id}/autonomy/apply", json=request,
                            headers={"Authorization": "Bearer " + token})
    assert response.status_code == http_status
    errors = []
    for route in MCP_ROUTES:
        result = _mcp_call(browser, token, route, "apply_autonomy_policy",
                           {"connection_id": connection_id, "request": request})
        assert result.get("isError") is True
        errors.append(True)
    return {"http_status": http_status, "compact_mcp_error": errors[0], "advanced_mcp_error": errors[1]}


def _delegation_checks(browser: TestClient, http: httpx.Client, issuer: str, values: dict[str, str],
                       operator: KnowledgeStore, connection_id: str, service_sub: str,
                       owner_sub: str, csrf: str) -> dict:
    """Custom model-free harness against real IdP JWTs and durable registry data."""
    bearer = {"Authorization": "Bearer " + _service_token(http, issuer, values)}
    snapshot = operator.get_snapshot("demo", connection_id)
    asset = snapshot["assets"][0]
    evidence_id = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"])
    submitted = browser.post("/agent/api/contract-assertions", headers=bearer, json={
        "connection_id": connection_id, "asset_id": asset["id"], "assertion_kind": "dataset_description",
        "payload": {"description": "One source record per disposable fixture order."},
        "evidence_ids": [evidence_id], "rationale": "The isolated order fixture supports the proposed description.",
        "expected_snapshot_id": snapshot["id"],
    })
    assert submitted.status_code == 202 and submitted.json()["approval_required"] is True
    proposal_id = submitted.json()["proposal"]["id"]
    assert operator.get_snapshot("demo", connection_id)["agent_ready"] is False
    browser_base = f"/api/knowledge/connections/{connection_id}"
    browser_headers = {"X-Knowledge-CSRF": csrf}
    decision = {"decision": "approve", "rationale": "The signed-in owner independently reviews this exact fixture description."}
    assert browser.post(browser_base + f"/proposals/{proposal_id}/decision", json=decision).status_code == 403
    reviewed = browser.post(browser_base + f"/proposals/{proposal_id}/decision", json=decision, headers=browser_headers)
    assert reviewed.status_code == 200
    snapshot = operator.get_snapshot("demo", connection_id)
    policy = {"schema_version": "knowledge-autonomy-policy-v1", "action": "publish_reviewed_unchanged",
              "subject": service_sub, "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
              "max_publications": 2, "purpose": "Allow this exact service account to publish unchanged reviewed fixture meaning."}
    drafted = browser.post(browser_base + "/autonomy/draft", json={"payload": policy}, headers=browser_headers)
    assert drafted.status_code == 201
    row = drafted.json()
    approval = {"revision": row["revision"], "expected_digest": row["digest"],
                "rationale": "The browser steward separately authorizes this bounded service delegation."}
    assert browser.post(browser_base + "/autonomy/approve", json=approval).status_code == 403
    approved = browser.post(browser_base + "/autonomy/approve", json=approval, headers=browser_headers)
    assert approved.status_code == 200 and approved.json()["approved_by"] == owner_sub
    agent_base = f"/agent/api/sources/{connection_id}/autonomy"
    for operation in ("draft", "approve", "revoke"):
        assert browser.post(agent_base + "/" + operation, json={}, headers=bearer).status_code == 404
    request = {"revision": row["revision"], "expected_digest": row["digest"],
               "expected_snapshot_id": snapshot["id"], "expected_snapshot_version": snapshot["version"]}
    token = _service_token(http, issuer, values)
    assert set(DELEGATION_SCOPES.split()).issubset(_claims(token)["scope"].split())
    assert _claims(token)["sub"] == service_sub and service_sub != owner_sub
    service_headers = {"Authorization": "Bearer " + token}
    current = browser.get(agent_base, headers=service_headers)
    assert current.status_code == 200 and current.json()["state"] == "active"
    applied = browser.post(agent_base + "/apply", headers=service_headers, json=request)
    assert applied.status_code == 200 and applied.json()["result"] == "published"
    run_id = applied.json()["run"]["id"]
    assert applied.json()["source_executed"] is False and applied.json()["run"]["actor"] == service_sub
    for route in MCP_ROUTES:
        current = _mcp_call(browser, token, route, "get_autonomy_policy", {"connection_id": connection_id})
        assert not current.get("isError") and current["structuredContent"]["state"] == "active"
        result = _mcp_call(browser, token, route, "apply_autonomy_policy",
                           {"connection_id": connection_id, "request": request})
        assert not result.get("isError") and result["structuredContent"]["result"] == "already_applied"
        assert result["structuredContent"]["run"]["id"] == run_id
    missing_scope = _assert_delegation_refused(
        browser, _service_token(http, issuer, values, scope="knowledge.agent knowledge.sources.manage"),
        connection_id, request, http_status=403,
    )
    wrong_subject = _assert_delegation_refused(
        browser, _token(http, issuer, values, "owner", scope="openid " + DELEGATION_SCOPES),
        connection_id, request, http_status=403,
    )
    operator.revoke_source_access("demo", connection_id, principal_type="user", principal_id=service_sub)
    removed_grant = _assert_delegation_refused(
        browser, _service_token(http, issuer, values), connection_id, request, http_status=404,
    )
    operator.grant_source_access("demo", connection_id, principal_type="user", principal_id=service_sub, role="manage")
    retry = browser.post(agent_base + "/apply", json=request,
                          headers={"Authorization": "Bearer " + _service_token(http, issuer, values)})
    assert retry.status_code == 200 and retry.json()["result"] == "already_applied"
    manual_withdrawal = browser.post(browser_base + "/publication/withdraw", headers=browser_headers,
                                    json={"reason": "The browser steward withdraws the delegated source publication."})
    assert manual_withdrawal.status_code == 200
    withdrawn_publication = _assert_delegation_refused(
        browser, _service_token(http, issuer, values), connection_id, request, http_status=409,
    )
    revoked = browser.post(browser_base + "/autonomy/revoke", headers=browser_headers, json={
        "revision": row["revision"], "expected_digest": row["digest"],
        "reason": "The browser steward independently withdraws the delegation policy.",
    })
    assert revoked.status_code == 200
    withdrawn_policy = _assert_delegation_refused(
        browser, _service_token(http, issuer, values), connection_id, request, http_status=409,
    )
    with operator.engine.connect() as conn:
        def rows(table):
            return conn.execute(select(table).where(table.c.tenant_id == "demo",
                                table.c.connection_id == connection_id).limit(3)).mappings().all()
        runs, revisions, publications, decisions = (rows(table) for table in (
            operator.autonomy_runs, operator.autonomy_revisions, operator.publications, operator.decisions,
        ))
    assert len(runs) == len(revisions) == len(publications) == len(decisions) == 1
    assert runs[0]["id"] == run_id and runs[0]["actor"] == publications[0]["published_by"] == service_sub
    assert runs[0]["owns_publication"] is True and revisions[0]["publication_count"] == 1
    assert revisions[0]["approved_by"] == owner_sub and publications[0]["revoked_at"] is not None
    assert decisions[0]["proposal_id"] == proposal_id and decisions[0]["decision"] == "approve"
    assert operator.get_snapshot("demo", connection_id)["agent_ready"] is False
    return {"result": "passed", "service_client": SERVICE_CLIENT, "grant_type": "client_credentials",
            "service_subject_matches_admin_and_persisted_actor": True, "browser_steward_approved": True,
            "http_apply": "published", "compact_mcp_apply": "already_applied", "advanced_mcp_apply": "already_applied",
            "mcp_protocol": "2026-07-28", "publication_count": 1, "run_count": 1, "independent_review_count": 1,
            "missing_autonomy_scope": missing_scope, "wrong_subject": wrong_subject, "revoked_direct_grant": removed_grant,
            "manual_publication_withdrawal": withdrawn_publication, "policy_withdrawal": withdrawn_policy,
            "retry_did_not_spend_usage": True, "new_meaning_automatically_approved": False,
            "source_executed": False, "harness": "model-free custom HTTP/MCP ASGI harness"}


def _run_checks(path: Path, issuer: str, pg_port: int, values: dict[str, str]) -> dict:
    os.environ["SSL_CERT_FILE"] = str(path / "tls.crt")
    database_url = (
        "postgresql+psycopg://knowledge_drill:"
        + quote(values["pg_password"], safe="")
        + f"@127.0.0.1:{pg_port}/knowledge_drill"
    )
    settings = {
        "KNOWLEDGE_AUTH_MODE": "oidc", "KNOWLEDGE_ENABLED_KINDS": "local_files",
        "KNOWLEDGE_AGENT_AUTONOMY": "1", "KNOWLEDGE_AGENT_SOURCE_MANAGEMENT": "0",
        "KNOWLEDGE_AGENT_ALLOWED_KINDS": "local_files", "KNOWLEDGE_AGENT_TOKEN": "",
        "DATABASE_URL": database_url,
        "KNOWLEDGE_OIDC_ISSUER": issuer, "KNOWLEDGE_OIDC_CLIENT_ID": "knowledge-web",
        "KNOWLEDGE_OIDC_CLIENT_SECRET": values["web_secret"],
        "KNOWLEDGE_OIDC_AUDIENCE": "knowledge-agent",
        "KNOWLEDGE_OIDC_JWKS_URI": issuer + "/protocol/openid-connect/certs",
        "KNOWLEDGE_OIDC_SESSION_KEY": values["session_key"],
        "KNOWLEDGE_OIDC_ALLOWED_GROUP": "knowledge-users",
        "KNOWLEDGE_OIDC_STEWARD_GROUP": "knowledge-stewards",
        "KNOWLEDGE_OIDC_LOGIN_SCOPES": "openid",
        "KNOWLEDGE_PUBLIC_URL": PUBLIC_URL,
        "KNOWLEDGE_SOURCE_POLICY": json.dumps({"version": 1, "sources": [{
            "tenant_id": "demo", "kind": "local_files",
            "selector": {"path": ".", "include": "*.csv"},
            "allowed_subjects": [], "allowed_groups": ["knowledge-stewards"],
        }]}),
    }
    # File overrides from the operator's shell must never select a customer
    # database, IdP, source policy or credential for this disposable drill.
    for name, value in settings.items():
        os.environ.pop(name + "_FILE", None)
        os.environ[name] = value
    store = KnowledgeStore(database_url=database_url, initialize_schema=False,
                           local_files_root=path / "data")
    try:
        migration = migrate("upgrade", store)
        assert migration["schema_matches"] is True
        source = store.create_connection("demo", kind="local_files", name="OIDC drill",
                                         config={"path": ".", "include": "*.csv"})
        store.refresh_connection("demo", source["id"])
        store.grant_source_access("demo", source["id"], principal_type="group",
                                  principal_id="knowledge-users", role="read")
        with httpx.Client(verify=str(path / "tls.crt"), timeout=10) as http:
            admin, admin_header = _keycloak_admin(http, issuer, values)
            owner = _token(http, issuer, values, "owner")
            reader = _token(http, issuer, values, "reader")
            unscoped = _token(http, issuer, values, "reader", scope="openid")
            wrong_audience = _token(http, issuer, values, "reader",
                                    client_id="knowledge-other")
            owner_sub = _claims(owner)["sub"]
            service_sub = _service_subject(http, admin, admin_header)
            os.environ["KNOWLEDGE_SOURCE_POLICY"] = json.dumps({"version": 1, "sources": [{
                "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "*.csv"},
                "allowed_subjects": [service_sub], "allowed_groups": ["knowledge-stewards"],
            }]})
            store.grant_source_access("demo", source["id"], principal_type="user",
                                      principal_id=owner_sub, role="manage")
            store.grant_source_access("demo", source["id"], principal_type="user",
                                      principal_id=service_sub, role="manage")
            # Import only after the disposable DB has migrated and all host
            # settings are pinned: platform's module-level app also opens a store.
            from knowledge_layer.platform import create_platform

            app = create_platform(store=store)
            with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as browser:
                endpoint = "/agent/api/sources"

                def sources(token: str | None) -> httpx.Response:
                    headers = {"Authorization": "Bearer " + token} if token else {}
                    return browser.get(endpoint, headers=headers)

                assert sources(None).status_code == 401
                assert len(sources(owner).json()["connections"]) == 1
                assert len(sources(reader).json()["connections"]) == 1
                assert sources(unscoped).status_code == 401
                assert sources(wrong_audience).status_code == 401
                parts = owner.split(".")
                signature = parts[2]
                parts[2] = ("A" if signature[0] != "A" else "B") + signature[1:]
                assert sources(".".join(parts)).status_code == 401
                mcp = browser.post("/agent/mcp/", headers={
                    "Authorization": "Bearer " + owner,
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18",
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "get_workspace", "arguments": {}}})
                assert mcp.status_code == 200 and "result" in mcp.json()

                signed_in = _browser_login(browser, http, "owner", values)
                assert signed_in.status_code == 303
                assert "Max-Age=900" in signed_in.headers.get("set-cookie", "")
                me = browser.get("/auth/me")
                assert me.status_code == 200 and me.json()["steward"] is True
                assert me.json()["subject"] == owner_sub
                assert browser.get("/api/knowledge/connections").status_code == 200
                operator = KnowledgeStore(database_url=database_url, initialize_schema=False)
                try:
                    delegation = _delegation_checks(browser, http, issuer, values, operator,
                                                    source["id"], service_sub, owner_sub, me.json()["csrf_token"])
                finally:
                    operator.close()
                assert browser.post("/auth/logout").status_code == 403
                assert browser.post("/auth/logout", headers={
                    "X-Knowledge-CSRF": me.json()["csrf_token"],
                }).status_code == 200
                assert browser.get("/auth/me").status_code == 401

                headers = {"Authorization": admin_header}
                # Keep the pre-change token live after the additional delegation
                # checks; expiry below still waits for a real signed JWT deadline.
                reader = _token(http, issuer, values, "reader")
                users = http.get(admin + "/users", params={"username": "reader"},
                                 headers=headers).json()
                groups = http.get(admin + "/groups", headers=headers).json()
                reader_id = next(item["id"] for item in users if item["username"] == "reader")
                group_id = next(item["id"] for item in groups if item["name"] == "knowledge-users")
                removed = http.delete(admin + f"/users/{reader_id}/groups/{group_id}", headers=headers)
                removed.raise_for_status()
                new_reader = _token(http, issuer, values, "reader")
                assert "knowledge-users" not in _claims(new_reader).get("groups", [])
                assert sources(new_reader).json()["connections"] == []
                assert len(sources(reader).json()["connections"]) == 1
                with httpx.Client(verify=str(path / "tls.crt"), timeout=10) as fresh_idp:
                    assert _browser_login(browser, fresh_idp, "reader", values).status_code == 403

                expires_at = int(_claims(reader)["exp"])
                time.sleep(max(0, expires_at - time.time() + 1))
                assert sources(reader).status_code == 401
            return {
                "evaluated_at": datetime.now(UTC).isoformat(), "result": "passed",
                "provider": KEYCLOAK_IMAGE, "schema_revision": migration["revision"],
                "browser_code_pkce": "passed", "browser_csrf_logout": "passed",
                "agent_mcp_jwt": "passed", "missing_scope_denied": True,
                "wrong_audience_denied": True, "invalid_signature_denied": True,
                "fresh_role_change_denied": True, "expired_token_denied": True,
                "preexpiry_jwt_role_staleness_observed": True,
                "source_count": 1,
                "delegation": delegation,
                "limits": "Local Keycloak dev realm, synthetic user and source, PostgreSQL on tmpfs; "
                          "custom model-free ASGI HTTP/MCP harness, not native Codex/Claude or customer qualification; "
                          "not a customer IdP, deployed HTTPS proxy, or source-native ACL test.",
            }
    finally:
        store.close()


def _run_disposable(path: Path, issuer: str, pg_port: int, idp_port: int,
                    pg_name: str, idp_name: str, values: dict[str, str]) -> dict:
    intended: list[str] = []
    stage = "launch_postgres"
    failure = None
    try:
        # A timed-out docker run may have created its container already.
        # Register our exact intended names before launching, then require the
        # nonce ownership label before removing anything during teardown.
        intended.append(pg_name)
        _docker("run", "--pull=never", "--rm", "-d", "--name", pg_name, "--memory", "512m", "--cpus", "1",
                "--label", OWNERSHIP_LABEL + "=" + pg_name,
                "-p", f"127.0.0.1:{pg_port}:5432", "--env-file", str(path / "pg.env"),
                "--tmpfs", "/var/lib/postgresql:rw,size=256m", POSTGRES_IMAGE)
        intended.append(idp_name)
        stage = "launch_keycloak"
        _docker("run", "--pull=never", "--rm", "-d", "--name", idp_name, "--user", "0", "--memory", "1g",
                "--label", OWNERSHIP_LABEL + "=" + idp_name,
                "--cpus", "2", "-p", f"127.0.0.1:{idp_port}:8443",
                "--env-file", str(path / "admin.env"),
                "-e", f"KC_HOSTNAME=https://localhost:{idp_port}",
                "-e", "KC_HTTPS_CERTIFICATE_FILE=/etc/knowledge/tls.crt",
                "-e", "KC_HTTPS_CERTIFICATE_KEY_FILE=/etc/knowledge/tls.key",
                "-v", f"{path}:/opt/keycloak/data/import:ro", "-v", f"{path}:/etc/knowledge:ro",
                KEYCLOAK_IMAGE, "start-dev", "--import-realm", "--https-port=8443")
        stage = "readiness"
        _wait_ready(issuer, path / "tls.crt", pg_name)
        source_digest = hashlib.sha256((path / "data" / "orders.csv").read_bytes()).digest()
        stage = "registry_checks"
        report = _run_checks(path, issuer, pg_port, values)
        assert hashlib.sha256((path / "data" / "orders.csv").read_bytes()).digest() == source_digest
        report["source_fixture_unchanged"] = True
    except Exception as exc:  # noqa: BLE001 - attach only a constant stage, never driver text
        failure = exc if isinstance(exc, DrillFailure) else DrillFailure("unclassified_error", stage)
        raise failure from None
    finally:
        try:
            _cleanup_targets(intended)
        except Exception as exc:  # noqa: BLE001 - cleanup must also sanitize arbitrary driver text
            cleanup = exc if isinstance(exc, DrillFailure) else DrillFailure("cleanup_failed", "cleanup")
            cleanup.previous_failure = failure
            raise cleanup from None
    report["cleanup"] = {"verified_absent": len(intended), "exact_nonce_labels_checked": True}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idp-port", type=int, default=18443)
    parser.add_argument("--postgres-port", type=int, default=15544)
    args = parser.parse_args()
    if not (1024 <= args.idp_port <= 65535 and 1024 <= args.postgres_port <= 65535
            and args.idp_port != args.postgres_port):
        parser.error("Use two distinct unprivileged loopback ports")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.disable(logging.INFO)
    local = ROOT / ".local"
    local.mkdir(mode=0o700, exist_ok=True)
    nonce = secrets.token_hex(8)
    idp_name, pg_name = f"knowledge-oidc-keycloak-{nonce}", f"knowledge-oidc-pg-{nonce}"
    issuer = f"https://localhost:{args.idp_port}/realms/knowledge-drill"
    stage = "bootstrap"
    try:
        _pull_known_images()
        with tempfile.TemporaryDirectory(prefix="oidc-drill-", dir=local) as dirname:
            path = Path(dirname)
            stage = "fixture"
            values = _fixture(path)
            stage = "registry_checks"
            report = _run_disposable(path, issuer, args.postgres_port, args.idp_port, pg_name, idp_name, values)
        # A success receipt is emitted only after owned containers and the
        # temporary credentials directory have been removed and verified.
        report["cleanup"]["temporary_credentials_removed"] = True
        report["runtime_budgets"] = {"image_pull_seconds_per_image": IMAGE_PULL_TIMEOUT_SECONDS,
                                     "launch_seconds_per_container": LAUNCH_TIMEOUT_SECONDS, "readiness_seconds": 90}
        stage = "report_write"
        output = local / "recovery"
        output.mkdir(mode=0o700, exist_ok=True)
        report_path = output / f"oidc-drill-{nonce}.json"
        fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        print(json.dumps({"result": report["result"], "report": str(report_path),
                          "schema_revision": report["schema_revision"]}))
    except Exception as exc:  # noqa: BLE001 - driver/HTTP exceptions can carry credentials
        print(json.dumps(_safe_failure(exc, stage)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
