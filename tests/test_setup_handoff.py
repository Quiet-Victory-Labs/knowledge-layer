"""Setup handoffs preserve the operator's origin and real development ports."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_layer.setup import create_setup_router


def _handoff(*, settings=None, origin="http://127.0.0.1:4591", mode="demo"):
    app = FastAPI()
    store = SimpleNamespace(count_connections=lambda _tenant: 0)
    app.include_router(create_setup_router(store, auth_mode=mode, settings=settings))
    with TestClient(app, base_url=origin) as client:
        response = client.get("/api/setup")
        assert response.status_code == 200
        return response.json()


def test_demo_setup_uses_actual_request_origin_when_no_override(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_PUBLIC_URL", raising=False)
    body = _handoff()
    assert body["public_url"] == "http://127.0.0.1:4591"
    assert body["agent"]["endpoint"] == "http://127.0.0.1:4591/agent/mcp/"
    assert 'url = "http://127.0.0.1:4591/agent/mcp/"' in body["agent"]["clients"]["codex"]


def test_demo_setup_respects_explicit_operator_origin(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_PUBLIC_URL", "http://127.0.0.1:4599/")
    assert _handoff()["agent"]["endpoint"] == "http://127.0.0.1:4599/agent/mcp/"


def test_managed_setup_uses_validated_settings_over_host_and_environment(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_PUBLIC_URL", "https://untrusted.test")
    body = _handoff(mode="oidc", settings=SimpleNamespace(public_url="https://knowledge.example.test"))
    assert body["agent"]["endpoint"] == "https://knowledge.example.test/agent/mcp/"
    assert body["agent"]["auth"] == "oidc"
