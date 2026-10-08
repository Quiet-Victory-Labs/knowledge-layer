"""The external agent endpoint must fail closed without its bearer token."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.mcp import _compact_term_context


def _client(token: str | None) -> TestClient:
    root = FastAPI()
    private = FastAPI()

    @private.get("/")
    def protected() -> dict[str, bool]:
        return {"ok": True}

    root.mount("/agent", AgentBearerBoundary(private, token))
    return TestClient(root)


def test_agent_endpoint_requires_correct_bearer_token():
    with _client("known-token") as client:
        assert client.get("/agent/").status_code == 401
        assert client.get("/agent/", headers={"Authorization": "Bearer wrong"}).status_code == 401
        authorized = client.get("/agent/", headers={"Authorization": "Bearer known-token"})
        assert authorized.status_code == 200
        assert authorized.json() == {"ok": True}


def test_agent_endpoint_stays_disabled_without_configured_token():
    with _client(None) as client:
        assert client.get("/agent/").status_code == 503


def test_http_agent_api_preserves_trust_and_omits_connection_config():
    class Store:
        def list_connections(self, tenant_id, *, limit=None):
            assert tenant_id == "demo"
            assert limit == 30
            return [{"id": "source-1", "name": "Invoices", "status": "ready", "config": {"password": "private"}}]

        def count_connections(self, tenant_id):
            assert tenant_id == "demo"
            return 1

        def page_connections(self, tenant_id, *, limit, cursor):
            assert limit == 30 and cursor is None
            return {"connections": self.list_connections(tenant_id, limit=30),
                    "total": self.count_connections(tenant_id), "next_cursor": None}

        def get_connection(self, tenant_id, connection_id):
            return self.list_connections(tenant_id)[0] if connection_id == "source-1" else None

        def get_context(self, tenant_id, concept_id=None, term=None, connection_id=None):
            assert tenant_id == "demo"
            if term == "invoice" or concept_id == "concept-invoice":
                return {"concept_id": "concept-invoice", "trust_state": "inferred", "evidence_ids": ["e-1"], "matches": [{"id": "concept-invoice"}]}
            return {"matches": []} if term else None

        def submit_proposal(self, tenant_id, connection_id, **kwargs):
            assert tenant_id == "demo" and connection_id == "source-1"
            return {"id": "proposal-1", "status": "pending_review", **kwargs}

    app = FastAPI()
    app.mount("/agent", AgentBearerBoundary(build_agent_api(Store()), "known-token"))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer known-token"}
        sources = client.get("/agent/sources", headers=headers).json()
        context = client.get("/agent/context?term=invoice", headers=headers).json()
        missing = client.get("/agent/context?term=unknown", headers=headers)
        denied_proposal = client.post("/agent/proposals", json={})
        proposal = client.post(
            "/agent/proposals",
            headers=headers,
            json={
                "connection_id": "source-1", "target_concept_id": "concept-invoice",
                "rationale": "The source documentation defines this business term.",
                "evidence_ids": ["e-1"], "proposed_definition": "An amount due from a buyer.",
            },
        )

        assert {key: sources["connections"][0][key] for key in ("id", "name", "status")} == {
            "id": "source-1", "name": "Invoices", "status": "ready",
        }
        assert sources["connections"][0]["agent_ready"] is False
        assert "config" not in sources["connections"][0]
    assert context["trust_state"] == "inferred"
    assert context["evidence_ids"] == ["e-1"]
    assert missing.status_code == 404
    assert denied_proposal.status_code == 401
    assert proposal.status_code == 202
    assert proposal.json()["proposal"]["status"] == "pending_review"


def test_term_lookup_compacts_related_graph_without_losing_evidence_handles():
    relation = {
        "id": "rel-1", "subject_id": "c1", "object_id": "c2", "predicate": "may_reference",
        "status": "candidate", "rationale": "long explanation" * 100,
    }
    match = {
        "concept": {"id": "c1", "name": "Asset", "status": "candidate", "private_extra": "x" * 1000},
        "trust_state": "inferred",
        "snapshot": {"id": "s1", "version": 2},
        "evidence_ids": ["e1"],
        "evidence": [{"id": "e1", "summary": "Asset header observed", "source_pointer": "assets.csv"}],
        "source_bindings": [{"connection_id": "source-1", "connection_name": "Assets", "config": "private"}],
        "relationships": [relation] * 20,
    }
    result = _compact_term_context({"term": "asset", "matches": [match] * 10})
    assert len(result["matches"]) == 3
    assert result["more_available"] is True
    assert result["matches"][0]["evidence_ids"] == ["e1"]
    assert result["matches"][0]["relationship_count"] == 20
    assert "rationale" not in result["matches"][0]["relationships"][0]
    assert "config" not in result["matches"][0]["source_bindings"][0]
