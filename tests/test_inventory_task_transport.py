"""Actual SDK/HTTP consumption across shared inventory receipt replicas."""

import asyncio
import json
import secrets

import pytest
from fastapi.testclient import TestClient
from test_context_receipt_transport import _mcp
from test_inventory_citations import setup_case

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.context_receipts import CONFIG_NAME
from knowledge_layer.inventory_concepts import InventoryConcepts
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore


@pytest.fixture
def company(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_NAME, raising=False)
    keys = tmp_path / "private-keys"
    keys.write_text(json.dumps({"version": 1, "registry_id": "inventory-wire-fixture", "active_key_id": "initial",
        "keys": [{"id": "initial", "secret": secrets.token_urlsafe(32)}]}))
    keys.chmod(0o600)
    monkeypatch.setenv(CONFIG_NAME + "_FILE", str(keys))
    case = setup_case(tmp_path)
    store, cid = case["store"], case["catalog"]
    concepts = InventoryConcepts(store)
    definition = ("Net revenue excludes refunds. " + "Reviewed finance scope. " * 70).strip()
    draft = concepts.propose("demo", cid, evidence_ref=case["ref"], name="Net revenue", definition=definition,
        concept_kind="metric_definition", rationale="The fixture owner interprets this exact catalog observation.")
    store.decide_proposal("demo", cid, draft["id"], decision="approve")
    concepts.publish("demo", cid, concept_id=draft["id"], expected_proposal_digest=draft["proposal_digest"],
                     rationale="The fixture steward publishes this reviewed meaning only.")
    peer = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.sqlite'}", local_files_root=tmp_path,
                          initialize_schema=False)
    try:
        yield {**case, "peer": peer, "concepts": concepts, "id": draft["id"], "definition": definition,
               "token": secrets.token_hex(32), "request": {"query": "Net revenue", "connection_ids": [cid],
                   "inventory_selection": [{"connection_id": cid, "concept_ids": [draft["id"]]}]}}
    finally:
        peer.close()
        store.close()


def _http(company, store):
    return TestClient(AgentBearerBoundary(build_agent_api(store), company["token"]),
                      headers={"Authorization": "Bearer " + company["token"]})


def test_shared_http_inventory_receipt_preserves_meaning_and_observes_withdrawal(company):
    with _http(company, company["store"]) as first, _http(company, company["peer"]) as second:
        issued = first.post("/task-context", json=company["request"])
        assert issued.status_code == 200, issued.text
        packet = issued.json()
        assert packet["schema_version"] == "task-context-v2"
        assert packet["answerability"] == "business_meaning_available"
        assert packet["sources"][0]["basis"] == "inventory"
        assert "snapshot_id" not in packet["sources"][0]
        assert packet["contexts"][0]["context"]["matches"][0]["concept"]["definition"] == company["definition"]
        assert packet["receipt"]["request"]["inventory_selection"] == company["request"]["inventory_selection"]
        checked = second.post("/task-context/revalidate", json=packet["receipt"])
        assert checked.status_code == 200 and checked.json()["status"] == "current"
        assert checked.json()["source_permissions_verified"] is False
        term = first.get("/context", params={"term": "Net revenue", "connection_id": company["catalog"]})
        assert term.status_code == 200 and term.json()["matches"][0]["concept"]["definition"] == company["definition"]
        found = first.get("/search", params={"q": "Net revenue"})
        assert found.status_code == 200 and found.json()["results"][0]["definition"] == company["definition"]
        assert found.json()["results"][0]["context"]["publication"]["proposal_digest"]
        bad_auth = first.post("/task-context", json=company["request"], headers={"Authorization": "Bearer wrong"})
        assert bad_auth.status_code == 401
        foreign = {**company["request"], "inventory_selection": [
            {"connection_id": company["target"], "concept_ids": [company["id"]]}]}
        assert first.post("/task-context", json=foreign).status_code == 422
        injected = {**company["request"], "inventory_selection": [
            {**company["request"]["inventory_selection"][0], "tenant_id": "foreign"}]}
        assert first.post("/task-context", json=injected).status_code == 422
        company["concepts"].withdraw("demo", company["catalog"], concept_id=company["id"],
                                      reason="The fixture steward withdrew business meaning.")
        assert second.post("/task-context/revalidate", json=packet["receipt"]).status_code == 404
        assert second.post("/task-context", json=company["request"]).status_code == 404


def test_http_keeps_all_twenty_explicit_handles_and_bounds_signed_receipt(company):
    ids = [company["id"]]
    for index in range(19):
        draft = company["concepts"].propose("demo", company["catalog"], evidence_ref=company["ref"],
            name=f"Finance concept {index}", definition=f"Reviewed local meaning for finance population {index}.",
            concept_kind="business_term", rationale="The fixture owner interprets this exact catalog observation.")
        company["store"].decide_proposal("demo", company["catalog"], draft["id"], decision="approve")
        company["concepts"].publish("demo", company["catalog"], concept_id=draft["id"],
            expected_proposal_digest=draft["proposal_digest"], rationale="Publish this reviewed fixture population.")
        ids.append(draft["id"])
    request = {**company["request"], "inventory_selection": [{"connection_id": company["catalog"], "concept_ids": ids}]}
    with _http(company, company["store"]) as first, _http(company, company["peer"]) as second:
        issued = first.post("/task-context", json=request)
        assert issued.status_code == 200, issued.text
        packet = issued.json()
        assert [item["id"] for item in packet["contexts"][0]["context"]["matches"]] == ids
        assert packet["sources"][0]["concept_ids"] == ids
        assert packet["query_coverage"] == "not_established"
        assert len(json.dumps(packet["receipt"], ensure_ascii=False).encode()) < 4096
        assert second.post("/task-context/revalidate", json=packet["receipt"]).status_code == 200


@pytest.mark.parametrize("builder", [build_quick_mcp_server, build_knowledge_mcp_server], ids=["compact", "advanced"])
def test_actual_sdk_inventory_context_crosses_replica_and_refuses_withdrawal(company, builder):
    async def consume():
        async with (_mcp(company["store"], builder, company["token"]) as first,
                    _mcp(company["peer"], builder, company["token"]) as second):
            issued = await first.call_tool("get_task_context", company["request"])
            assert not issued.is_error, issued.content
            packet = issued.structured_content
            match = packet["contexts"][0]["context"]["matches"][0]
            assert match["concept"]["definition"] == company["definition"]
            assert match["evidence_ref"] == company["ref"] and match["publication_state"] == "published"
            assert not match["locally_executable"] and not match["source_agent_ready"]
            assert packet["execution_state"] == "not_executed" and not packet["rules_enforced"]
            term_tool = "resolve_context" if builder == build_quick_mcp_server else "resolve_business_term"
            term = await first.call_tool(term_tool, {"term": "Net revenue", "connection_id": company["catalog"]})
            assert not term.is_error
            assert term.structured_content["matches"][0]["concept"]["definition"] == company["definition"]
            if builder == build_knowledge_mcp_server:
                found = await first.call_tool("search_knowledge", {"query": "Net revenue"})
                assert not found.is_error
                result = found.structured_content["results"][0]
                assert result["definition"] == result["context"]["concept"]["definition"] == company["definition"]
                assert result["evidence_ref"] == company["ref"]
            checked = await second.call_tool("revalidate_task_context", {"receipt": packet["receipt"]})
            assert not checked.is_error and checked.structured_content["status"] == "current"
            company["concepts"].withdraw("demo", company["catalog"], concept_id=company["id"],
                                         reason="The fixture steward withdrew this inventory meaning.")
            refused = await second.call_tool("revalidate_task_context", {"receipt": packet["receipt"]})
            assert refused.is_error and refused.structured_content is None
    asyncio.run(consume())
