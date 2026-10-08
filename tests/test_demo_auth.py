"""Demo browser access is explicit and does not bypass the agent boundary."""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from knowledge_layer.platform import create_platform
from knowledge_layer.store import KnowledgeStore


@pytest.fixture(params=[False, True], ids=["api-only", "with-web"])
def demo_client(monkeypatch, tmp_path, request):
    monkeypatch.setenv("KNOWLEDGE_AUTH_MODE", "demo")
    monkeypatch.setenv("KNOWLEDGE_AGENT_AUTONOMY", "0")
    monkeypatch.setenv("KNOWLEDGE_AGENT_SOURCE_MANAGEMENT", "0")
    monkeypatch.delenv("KNOWLEDGE_WEB_DIST", raising=False)
    if request.param:
        (tmp_path / "index.html").write_text("<html>Demo</html>", encoding="utf-8")
        monkeypatch.setenv("KNOWLEDGE_WEB_DIST", str(tmp_path))
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    agent_token = secrets.token_urlsafe(32)
    try:
        app = create_platform(store=store, agent_token=agent_token)
        with TestClient(app) as client:
            yield client, agent_token
    finally:
        store.close()


def test_demo_session_matches_status_without_issuing_credentials(demo_client):
    client, _token = demo_client
    status = client.get("/auth/status")
    assert status.status_code == 200
    assert status.json()["mode"] == "demo"
    assert status.json()["authenticated"] is True
    response = client.get("/auth/me")
    assert response.status_code == 200
    assert response.json() == {"mode": "demo", "subject": "operator", "groups": [],
                               "steward": True, "csrf_token": None}
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert not client.cookies


def test_demo_browser_session_does_not_authenticate_agents(demo_client):
    client, agent_token = demo_client
    assert client.get("/auth/me").status_code == 200
    created = client.post("/api/knowledge/connections", json={
        "name": "Demo files", "kind": "local_files", "config": {"path": "."},
    })
    assert created.status_code == 201, created.text
    assert client.get("/agent/api/sources").status_code == 401
    assert client.get("/agent/api/sources", headers={
        "Authorization": "Bearer operator",
    }).status_code == 401
    authorized = client.get("/agent/api/sources", headers={
        "Authorization": "Bearer " + agent_token,
    })
    assert authorized.status_code == 200
    assert len(authorized.json()["connections"]) == 1
