"""The demo bearer boundary honors the same operator file references as the CLI."""

from fastapi.testclient import TestClient

from knowledge_layer.store import KnowledgeStore


def test_platform_uses_operator_agent_token_file(tmp_path, monkeypatch):
    from knowledge_layer.platform import create_platform

    token = tmp_path / "agent-token"
    token.write_text("synthetic-test-bearer\n", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("KNOWLEDGE_AUTH_MODE", "demo")
    monkeypatch.delenv("KNOWLEDGE_AGENT_TOKEN", raising=False)  # pragma: allowlist secret - operator reference
    monkeypatch.setenv("KNOWLEDGE_AGENT_TOKEN_FILE", str(token))  # pragma: allowlist secret - operator reference
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    try:
        with TestClient(create_platform(store=store)) as client:
            assert client.get("/agent/api/guidance/resolve").status_code == 401
            denied = client.get("/agent/api/guidance/resolve", headers={"Authorization": "Bearer incorrect"})
            assert denied.status_code == 401
            result = client.get("/agent/api/guidance/resolve", headers={"Authorization": "Bearer synthetic-test-bearer"})
            assert result.status_code == 200 and result.json()["status"] == "empty"
    finally:
        store.close()
