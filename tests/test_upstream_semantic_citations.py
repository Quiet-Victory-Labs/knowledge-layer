"""Imported enterprise definitions govern a different source only through both reviews."""

import asyncio
import json

import httpx
import pytest
from adapter_response_fixture import streamed_response
from fastapi.testclient import TestClient

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.connectors import external_adapter
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.federated import cited_upstream_definitions, federated_evidence_graph
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeConflict, KnowledgeStore


@pytest.fixture(params=["metric", "glossary_term"])
def cited_import(tmp_path, monkeypatch, request):
    (tmp_path / "warehouse").mkdir()
    (tmp_path / "warehouse" / "orders.csv").write_text("order_id,margin_amount\nO-1,20\n", encoding="utf-8")
    native = {
        "id": "margin-17", "kind": request.param, "namespace": "retail",
        "definition": "Margin amount on one booked order, excluding canceled and returned orders. Native-source-only-phrase-17.",
        "version": None, "source_observed_at": None, "owner": None, "authority": None,
        "reference": "catalog/retail/terms/margin-17", "review_status": "approved",
        "unit": "USD", "formula": "net_sales - cost_of_goods",
    }
    uncited = {**native, "namespace": "wholesale", "definition": "Wholesale margin has independently scoped meaning."}
    payload = {"protocol_version": "1.2", "listing_complete": True, "assets": [
        {"name": "Margin per booked order", "pointer": "retail/margin-17", "asset_type": "semantic_definition",
         "schema": [], "upstream_definition": native},
        {"name": "Margin per booked order", "pointer": "wholesale/margin-17", "asset_type": "semantic_definition",
         "schema": [], "upstream_definition": uncited},
    ]}
    monkeypatch.setenv("KNOWLEDGE_SOURCE_CITED_IMPORT", json.dumps({
        "endpoint": "https://catalog-adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["definitions"], "protocol_version": "1.2",
    }))
    real_client = httpx.Client
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, json=payload)), **kwargs,
    ))
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    warehouse = store.create_connection("demo", kind="local_files", name="Warehouse", config={"path": "warehouse"})
    imported = store.create_connection("demo", kind="external_adapter", name="Enterprise glossary", config={
        "secret_ref": "KNOWLEDGE_SOURCE_CITED_IMPORT", "scope": "definitions",  # pragma: allowlist secret - operator reference
    })
    local = store.refresh_connection("demo", warehouse["id"])["snapshot"]
    store.refresh_connection("demo", imported["id"])
    asset = local["assets"][0]
    handle = next(item["id"] for item in local["evidence"] if item.get("asset_id") == asset["id"]
                  and (item.get("details") or {}).get("field") == "margin_amount")
    try:
        yield {"store": store, "warehouse": warehouse["id"], "imported": imported["id"], "asset": asset["id"],
               "handle": handle, "native": native, "payload": payload}
    finally:
        store.close()


def _meaning(fixture, definition="Margin per booked order excludes canceled and returned orders."):
    store, cid = fixture["store"], fixture["imported"]
    snapshot = store.get_snapshot("demo", cid)
    concept = next(item for item in snapshot["concepts"] if item["upstream_definition"]["namespace"] == "retail")
    proposal = store.submit_proposal(
        "demo", cid, target_concept_id=concept["id"], proposed_definition=definition,
        evidence_ids=concept["evidence_ids"], rationale="A steward reviewed the exact retail native definition.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", cid, proposal["id"], decision="approve")
    fixture["meaning_proposal"] = proposal["id"]
    return concept["id"]


def _assertion(fixture, kind="field_description", *, approve=True):
    store, cid = fixture["store"], fixture["warehouse"]
    upstream = store.get_snapshot("demo", fixture["imported"])
    concept = next(item for item in upstream["concepts"] if item["upstream_definition"]["namespace"] == "retail")
    ref = {"connection_id": fixture["imported"], "snapshot_id": upstream["id"],
           "evidence_id": concept["evidence_ids"][0]}
    if kind == "metric":
        payload = {"name": "Booked margin", "description": "Margin on booked orders, excluding canceled and returned orders.",
                   "measure_field": "margin_amount", "aggregation": "sum", "unit": "USD"}
    else:
        payload = {"field": "margin_amount", "description": "Margin on one booked order, excluding canceled and returned orders."}
    proposal = store.submit_contract_assertion(
        "demo", cid, assertion_kind=kind, asset_id=fixture["asset"], payload=payload,
        evidence_ids=[fixture["handle"]], evidence_refs=[ref],
        rationale="This explicit current warehouse field is governed by the separately reviewed retail definition.",
        expected_snapshot_id=store.get_snapshot("demo", cid)["id"],
    )
    fixture["ref"] = ref
    fixture["assertion"] = proposal["id"]
    if approve:
        store.decide_proposal("demo", cid, proposal["id"], decision="approve")
    return proposal


@pytest.mark.parametrize("kind", ["field_description", "metric"])
def test_imported_meaning_and_warehouse_contract_require_both_reviews(cited_import, kind):
    fixture = cited_import
    store, source = fixture["store"], fixture["warehouse"]
    concept_id = _meaning(fixture)
    _assertion(fixture, kind, approve=False)
    assert cited_upstream_definitions(store, tenant_id="demo", connection_id=source) == []
    pending = store.get_context("demo", term="Margin per booked order", connection_id=source)
    assert pending["cited_upstream_definitions"] == []
    store.decide_proposal("demo", source, fixture["assertion"], decision="approve")
    contract = semantic_contract(store, tenant_id="demo", connection_id=source)
    citation, = contract["cited_upstream_definitions"]
    assert citation["concept_id"] == concept_id and citation["term"] == "Margin per booked order"
    assert citation["definition"] == "Margin per booked order excludes canceled and returned orders."
    assert citation["definition_status"] == "approved" and citation["trust_state"] == "owner_reviewed"
    assert citation["definition_proposal_id"] == fixture["meaning_proposal"]
    assert citation["upstream_definition"] == {**fixture["native"], "name": "Margin per booked order"}
    assert citation["upstream_definition"]["version"] is None
    assert citation["upstream_definition"]["owner"] is None and citation["upstream_definition"]["source_observed_at"] is None
    assert citation["evidence_ref"] == fixture["ref"] and fixture["ref"]["evidence_id"] in citation["evidence_ids"]
    current_import = store.get_snapshot("demo", fixture["imported"])
    retail_asset = next(item for item in current_import["assets"] if item["upstream_definition"]["namespace"] == "retail")
    assert citation["source_revision"] == retail_asset["revision"]
    assert citation["source_pointer"] == retail_asset["source_pointer"]
    assert citation["source_assertion"]["assertion_id"] == fixture["assertion"]
    assert citation["source_assertion"]["asset_id"] == fixture["asset"]
    assert citation["source_assertion"]["field"] == "margin_amount"
    assert citation["executable"] is False and citation["certified"] is False
    assert contract["cited_glossary"] == []  # Imported meanings stay a separate collection.
    context = store.get_context("demo", term="Margin per booked order", connection_id=source)
    assert context["matches"] == [] and context["cited_upstream_definitions"] == [citation]
    graph = federated_evidence_graph(store, tenant_id="demo", connection_id=source)
    assert graph["linked_assertions"] == 1
    assert any(edge["predicate"] == "grounded_in" and edge["status"] == "approved" for edge in graph["edges"])
    if kind == "metric":
        assert contract["metrics"][0]["aggregation"] == "sum"
        assert contract["metrics"][0]["query_guidance"]["execution"]["execution_verified"] is False
    else:
        assert contract["datasets"][0]["fields"][1]["description_status"] == "approved"


def test_upstream_approved_label_is_evidence_for_pending_proposal_only(cited_import):
    fixture = cited_import
    proposal = _assertion(fixture, approve=False)
    store = fixture["store"]
    assert proposal["status"] == "pending_review"
    assert fixture["native"]["review_status"] == "approved"
    contract = semantic_contract(store, tenant_id="demo", connection_id=fixture["warehouse"])
    assert contract["cited_upstream_definitions"] == [] and contract["approved_assertions"] == []
    assert contract["pending_assertions"][0]["evidence_refs"] == [fixture["ref"]]
    with pytest.raises(KnowledgeConflict):
        store.decide_proposal("demo", fixture["warehouse"], proposal["id"], decision="approve")
    context = store.get_context("demo", term="Margin per booked order", connection_id=fixture["warehouse"])
    assert context["cited_upstream_definitions"] == []


@pytest.mark.parametrize("version", [None, "native-revision-7"])
def test_reused_or_missing_native_version_change_stales_both_reviews(cited_import, version):
    fixture = cited_import
    fixture["native"]["version"] = version
    fixture["store"].refresh_connection("demo", fixture["imported"])
    _meaning(fixture)
    _assertion(fixture, "metric")
    before = fixture["store"].get_snapshot("demo", fixture["imported"])
    fixture["native"]["definition"] = "Margin now includes canceled and returned orders."
    changed = fixture["store"].refresh_connection("demo", fixture["imported"])["snapshot"]
    assert changed["id"] != before["id"]
    assert next(item for item in changed["proposals"] if item["id"] == fixture["meaning_proposal"])["status"] == "stale_review"
    _no_authority(fixture)


def _no_authority(fixture):
    store, source = fixture["store"], fixture["warehouse"]
    contract = semantic_contract(store, tenant_id="demo", connection_id=source)
    assert contract["cited_upstream_definitions"] == [] and contract["approved_assertions"] == [] and contract["metrics"] == []
    assert len(contract["stale_assertions"]) == 1
    assert contract["stale_assertions"][0]["effective_on_current_snapshot"] is False
    assert "description_status" not in contract["datasets"][0]["fields"][1]
    context = store.get_context("demo", term="Margin per booked order", connection_id=source)
    assert context["cited_upstream_definitions"] == []
    assert not any(item.get("candidate_type") == "cited_upstream_definition" for item in context["retrieval_candidates"])
    assert "Native-source-only-phrase-17" not in json.dumps(context)
    graph = federated_evidence_graph(store, tenant_id="demo", connection_id=source)
    assert graph["linked_assertions"] == 0 and graph["stale_citations"] == 1
    assert not any(edge["predicate"] == "grounded_in" for edge in graph["edges"])


@pytest.mark.parametrize("invalidation", ["removal", "meaning_archive", "source_error"])
def test_removed_archived_or_unavailable_import_suspends_warehouse_assertion(cited_import, invalidation):
    fixture = cited_import
    _meaning(fixture)
    _assertion(fixture)
    if invalidation == "removal":
        fixture["payload"]["assets"] = fixture["payload"]["assets"][1:]
        fixture["store"].refresh_connection("demo", fixture["imported"])
    elif invalidation == "meaning_archive":
        fixture["store"].archive_proposals("demo", fixture["imported"], proposal_ids=[fixture["meaning_proposal"]],
                                          reason="The source steward withdrew this meaning review.")
    else:
        fixture["payload"]["listing_complete"] = False
        fixture["store"].refresh_connection("demo", fixture["imported"])
        assert fixture["store"].get_connection("demo", fixture["imported"])["status"] == "error"
    _no_authority(fixture)


@pytest.mark.parametrize("revoked", ["imported", "warehouse"])
def test_each_source_grant_is_required_and_denial_leaks_no_imported_meaning(cited_import, revoked):
    fixture = cited_import
    _meaning(fixture)
    _assertion(fixture)
    store = fixture["store"]
    for cid in (fixture["warehouse"], fixture["imported"]):
        store.grant_source_access("demo", cid, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    reader = Principal("reader", frozenset(), frozenset())
    with principal_scope(reader):
        assert semantic_contract(store, tenant_id="demo", connection_id=fixture["warehouse"])["cited_upstream_definitions"]
    store.access_enforced = False
    store.revoke_source_access("demo", fixture[revoked], principal_type="user", principal_id="reader")
    store.access_enforced = True
    with principal_scope(reader):
        if revoked == "warehouse":
            assert store.get_context("demo", term="Margin per booked order", connection_id=fixture["warehouse"]) is None
            with pytest.raises(ValueError, match="not found"):
                semantic_contract(store, tenant_id="demo", connection_id=fixture["warehouse"])
        else:
            contract = semantic_contract(store, tenant_id="demo", connection_id=fixture["warehouse"])
            assert contract["cited_upstream_definitions"] == []
            assert contract["approved_assertions"] == [] and contract["stale_assertions"] == []
            assert fixture["imported"] not in json.dumps(contract)
            assert "Native-source-only-phrase-17" not in json.dumps(contract)
            assert store.get_snapshot("demo", fixture["warehouse"])["dependency_unavailable"] is True
            assert "Native-source-only-phrase-17" not in json.dumps(semantic_contract(
                store, tenant_id="demo", connection_id=fixture["warehouse"]))


def test_long_approved_native_and_local_definitions_are_exact_in_http_and_compact_mcp(cited_import):
    fixture = cited_import
    fixture["native"]["definition"] += "\n" + ("Native scope details. " * 100).rstrip()
    fixture["store"].refresh_connection("demo", fixture["imported"])
    definition = "Margin per booked order. " + ("Reviewed scope details. " * 100).rstrip()
    _meaning(fixture, definition)
    _assertion(fixture)
    store, source = fixture["store"], fixture["warehouse"]
    expected = semantic_contract(store, tenant_id="demo", connection_id=source)["cited_upstream_definitions"]
    assert expected[0]["definition"] == definition
    assert expected[0]["upstream_definition"]["definition"] == fixture["native"]["definition"]
    with TestClient(build_agent_api(store)) as client:
        response = client.get("/context", params={"term": "Margin per booked order", "connection_id": source})
        assert response.status_code == 200 and response.json()["cited_upstream_definitions"] == expected
        response = client.get(f"/sources/{source}/contract")
        assert response.status_code == 200 and response.json()["cited_upstream_definitions"] == expected

    async def check_tools():
        server = build_quick_mcp_server(store)
        result = (await server.call_tool("resolve_context", {"term": "Margin per booked order", "connection_id": source})).structured_content
        assert result["cited_upstream_definitions"] == expected
        result = (await server.call_tool("get_contract", {"connection_id": source, "asset_id": fixture["asset"]})).structured_content
        assert result["cited_upstream_definitions"] == expected
    asyncio.run(check_tools())
