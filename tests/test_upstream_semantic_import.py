"""Native enterprise meaning stays observed, source-scoped, and review-bound."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from adapter_response_fixture import streamed_response
from fastapi.testclient import TestClient

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.connectors import external_adapter
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.curation import curate_source
from knowledge_layer.mcp import _compact_term_context
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeStore


def _asset(namespace="retail"):
    return {
        "name": "Revenue", "pointer": f"definitions/{namespace}/revenue", "asset_type": "semantic_definition", "schema": [],
        "upstream_definition": {
            "id": "native-revenue-17", "kind": "glossary_term", "namespace": namespace,
            "definition": "Revenue is booked sales excluding tax and returned orders.", "version": "published-7",
            "source_observed_at": "2026-10-03T12:00:00Z", "owner": "Retail finance",
            "authority": "Retail business glossary", "reference": "https://catalog.example/terms/revenue-17",
            "review_status": "published",
            "unit": "USD", "formula": "booked_sales - tax - returned_orders",
        },
    }


@pytest.fixture
def adapter(monkeypatch):
    payload = {"protocol_version": "1.2", "listing_complete": True, "assets": [_asset()]}
    profile = {"endpoint": "https://adapter.example/discover", "provider": "enterprise_catalog",
               "allowed_scopes": ["business_definitions"], "protocol_version": "1.2"}
    monkeypatch.setenv("KNOWLEDGE_SOURCE_SEMANTICS", json.dumps(profile))
    real_client = httpx.Client

    def respond(request):
        assert json.loads(request.content)["protocol_version"] == payload["protocol_version"]
        return streamed_response(200, json=payload)

    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    return {"secret_ref": "KNOWLEDGE_SOURCE_SEMANTICS", "scope": "business_definitions"}, payload, profile  # pragma: allowlist secret - operator reference


@pytest.fixture
def store():
    registry = KnowledgeStore(database_url="sqlite:///:memory:")
    yield registry
    registry.close()


def _connect(store, config):
    source = store.create_connection("demo", kind="external_adapter", name="Enterprise meaning", config=config)
    return source["id"], store.refresh_connection("demo", source["id"])["snapshot"]


@pytest.mark.parametrize("kind", sorted(external_adapter.UPSTREAM_DEFINITION_KINDS))
def test_native_definition_kinds_remain_observed_candidates(adapter, kind):
    config, payload, _ = adapter
    payload["assets"][0]["upstream_definition"]["kind"] = kind
    snapshot = curate_source("external_adapter", config)
    assert snapshot["discovery_complete"] is True
    assert snapshot["agent_ready"] is False and snapshot["meaning_certified"] is False
    assert snapshot["metrics"] == [] and snapshot["relationships"] == [] and snapshot["proposals"] == []
    concept, = snapshot["concepts"]
    assert concept["semantic_type"] == "upstream_" + kind
    assert concept["definition"] is None and concept["status"] == "candidate"
    assert concept["upstream_definition"]["review_status"] == "published"
    assert concept["candidate_definitions"][0]["text"] == payload["assets"][0]["upstream_definition"]["definition"]
    assert any(item["evidence_type"] == "upstream_semantic_definition" for item in snapshot["evidence"])


@pytest.mark.parametrize("kind", sorted(external_adapter.UPSTREAM_DEFINITION_KINDS))
def test_native_definitions_enter_evidence_bound_meaning_work(adapter, store, kind):
    from knowledge_layer.work import list_curation_work

    config, payload, _ = adapter
    payload["assets"][0]["upstream_definition"]["kind"] = kind
    cid, snapshot = _connect(store, config)
    packet = list_curation_work(store, tenant_id="demo", connection_id=cid)["work"][0]
    assert packet["snapshot_id"] == snapshot["id"]
    assert packet["candidate"]["upstream_definition"] == snapshot["concepts"][0]["upstream_definition"]
    assert packet["evidence_ids"] == snapshot["concepts"][0]["evidence_ids"]
    assert packet["candidate"]["upstream_definition"]["review_status"] == "published"
    assert packet["candidate"]["status"] == "candidate"


@pytest.mark.parametrize("kind", sorted(external_adapter.UPSTREAM_DEFINITION_KINDS))
def test_shacl_accepts_observed_native_definitions_without_local_approval(adapter, store, kind):
    pytest.importorskip("pyshacl")
    from knowledge_layer.ontology import project_snapshot
    from knowledge_layer.rdf_validation import validate_graph

    config, payload, _ = adapter
    payload["assets"][0]["upstream_definition"]["kind"] = kind
    discovered = curate_source("external_adapter", config)
    source_id, stored = _connect(store, config)
    for snapshot in [discovered, stored]:
        report = validate_graph(project_snapshot(snapshot, tenant_id="demo", connection_id=source_id))
        assert report["conforms"] is True, report
        assert snapshot["concepts"][0]["definition"] is None
        assert snapshot["agent_ready"] is False


def test_native_identity_provenance_and_definition_reach_consumers(adapter, store):
    config, payload, _ = adapter
    payload["assets"][0]["upstream_definition"]["definition"] += "\n" + "Scope details. " * 200
    source_id, snapshot = _connect(store, config)
    native = payload["assets"][0]["upstream_definition"]
    contract = semantic_contract(store, tenant_id="demo", connection_id=source_id)
    record, = contract["upstream_definitions"]
    assert all(record[key] == value for key, value in native.items())
    assert record["name"] == "Revenue" and record["source_revision"] == snapshot["assets"][0]["revision"]
    assert record["status"] == "observed_upstream" and record["definition_status"] == "upstream_declared"
    assert record["kl_definition_status"] == "candidate" and record["kl_definition"] is None
    assert record["executable"] is False and record["certified"] is False
    assert contract["metrics"] == [] and contract["approved_assertions"] == []
    context = _compact_term_context(store.get_context("demo", term="Revenue", connection_id=source_id))
    concept = context["matches"][0]["concept"]
    assert concept["upstream_definition"] == {**native, "name": "Revenue"}
    assert concept["candidate_definitions"][0]["text"] == native["definition"]
    with TestClient(build_agent_api(store)) as client:
        response = client.get("/context", params={"term": "Revenue", "connection_id": source_id})
        assert response.status_code == 200
        assert response.json()["matches"][0]["concept"]["upstream_definition"] == concept["upstream_definition"]
        response = client.get(f"/sources/{source_id}/contract")
        assert response.status_code == 200
        assert response.json()["upstream_definitions"] == contract["upstream_definitions"]
    async def check_compact_tools():
        server = build_quick_mcp_server(store)
        context = (await server.call_tool("resolve_context", {"term": "Revenue", "connection_id": source_id})).structured_content
        assert context["matches"][0]["concept"]["upstream_definition"] == concept["upstream_definition"]
        result = (await server.call_tool("get_contract", {"connection_id": source_id, "asset_id": snapshot["assets"][0]["id"]})).structured_content
        assert result["upstream_definitions"] == contract["upstream_definitions"]
    asyncio.run(check_compact_tools())


def test_same_name_and_native_id_in_distinct_business_namespaces_never_merge(adapter, store):
    config, payload, _ = adapter
    other = _asset("wholesale")
    other["upstream_definition"]["definition"] = "Revenue includes contracted wholesale shipments."
    payload["assets"].append(other)
    source_id, snapshot = _connect(store, config)
    assert len(snapshot["concepts"]) == 2
    assert len({item["id"] for item in snapshot["concepts"]}) == 2
    assert snapshot["relationships"] == [] and snapshot["proposals"] == []
    contexts = store.get_context("demo", term="Revenue", connection_id=source_id)["matches"]
    assert {item["concept"]["upstream_definition"]["namespace"] for item in contexts} == {"retail", "wholesale"}
    assert all(item["cross_source_candidates"] == [] for item in contexts)


@pytest.mark.parametrize("version", ["published-7", None])
def test_reused_or_unknown_native_version_cannot_hide_definition_change(adapter, store, version):
    config, payload, _ = adapter
    payload["assets"][0]["upstream_definition"]["version"] = version
    source_id, first = _connect(store, config)
    concept, = first["concepts"]
    proposal = store.submit_proposal("demo", source_id, target_concept_id=concept["id"],
                                    proposed_definition=concept["candidate_definitions"][0]["text"],
                                    evidence_ids=concept["evidence_ids"], rationale="Steward checked the native business definition.",
                                    expected_snapshot_id=first["id"])
    assert proposal["status"] == "pending_review"
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve", rationale="Source authority checked.")
    assert store.get_snapshot("demo", source_id)["concepts"][0]["definition_status"] == "approved"
    payload["assets"][0]["upstream_definition"]["definition"] = "Revenue now includes tax and returned orders."
    changed = store.refresh_connection("demo", source_id)["snapshot"]
    assert changed["concepts"][0]["id"] == concept["id"]
    assert changed["assets"][0]["revision"] != first["assets"][0]["revision"]
    assert changed["concepts"][0]["evidence_ids"] != concept["evidence_ids"]
    assert changed["concepts"][0]["upstream_definition"]["version"] == version
    assert changed["concepts"][0]["definition"] is None
    old = next(item for item in changed["proposals"] if item["id"] == proposal["id"])
    assert old["status"] == "stale_review" and old["effective_on_current_snapshot"] is False
    assert semantic_contract(store, tenant_id="demo", connection_id=source_id)["upstream_definitions"][0]["kl_definition_status"] == "candidate"
    prior = store.get_snapshot("demo", source_id, version=1)
    assert prior["concepts"][0]["candidate_definitions"][0]["text"] == concept["candidate_definitions"][0]["text"]


def test_complete_listing_deletion_removes_current_terms_but_keeps_history(adapter, store):
    config, payload, _ = adapter
    source_id, first = _connect(store, config)
    payload["assets"] = []
    current = store.refresh_connection("demo", source_id)["snapshot"]
    assert current["discovery_complete"] is True and current["assets"] == [] and current["concepts"] == []
    assert store.get_context("demo", term="Revenue", connection_id=source_id)["matches"] == []
    assert semantic_contract(store, tenant_id="demo", connection_id=source_id)["upstream_definitions"] == []
    assert store.get_snapshot("demo", source_id, version=1)["concepts"][0]["id"] == first["concepts"][0]["id"]


def test_absent_optional_provenance_stays_unknown_and_clock_is_not_content(adapter):
    config, payload, _ = adapter
    native = payload["assets"][0]["upstream_definition"]
    for key in ("source_observed_at", "owner", "authority", "reference", "review_status", "unit", "formula"):
        native.pop(key)
    first = curate_source("external_adapter", config)
    upstream = first["concepts"][0]["upstream_definition"]
    assert all(upstream[key] is None for key in ("source_observed_at", "owner", "authority", "reference", "review_status", "unit", "formula"))
    native["source_observed_at"] = "2026-10-03T13:00:00Z"
    observed = curate_source("external_adapter", config)
    assert observed["assets"][0]["revision"] == first["assets"][0]["revision"]
    assert observed["concepts"][0]["evidence_ids"] == first["concepts"][0]["evidence_ids"]
    assert observed["concepts"][0]["upstream_definition"]["source_observed_at"] == native["source_observed_at"]


def test_changed_source_pointer_invalidates_review_without_changing_native_identity(adapter, store):
    config, payload, _ = adapter
    source_id, first = _connect(store, config)
    concept = first["concepts"][0]
    proposal = store.submit_proposal("demo", source_id, target_concept_id=concept["id"],
                                    proposed_definition=concept["candidate_definitions"][0]["text"],
                                    evidence_ids=concept["evidence_ids"], rationale="Steward checked this exact source reference.")
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve")
    payload["assets"][0]["pointer"] = "definitions/retail/new-location"
    changed = store.refresh_connection("demo", source_id)["snapshot"]
    assert changed["concepts"][0]["id"] == concept["id"]
    assert changed["concepts"][0]["definition"] is None
    assert next(item for item in changed["proposals"] if item["id"] == proposal["id"])["status"] == "stale_review"


@pytest.mark.parametrize("field,value", [("formula", "booked_sales - tax"), ("unit", "EUR"),
                                        ("owner", "Wholesale finance"), ("review_status", "withdrawn")])
def test_changed_native_meaning_or_authority_changes_binding_with_reused_version(adapter, field, value):
    config, payload, _ = adapter
    first = curate_source("external_adapter", config)
    payload["assets"][0]["upstream_definition"][field] = value
    changed = curate_source("external_adapter", config)
    assert changed["assets"][0]["revision"] != first["assets"][0]["revision"]
    assert changed["concepts"][0]["evidence_ids"] != first["concepts"][0]["evidence_ids"]


@pytest.mark.parametrize("field,value", [
    ("id", " "), ("kind", "invented_kind"), ("namespace", ""), ("definition", "x" * 4001),
    ("definition", "bad\x00meaning"), ("version", 7), ("owner", []), ("review_status", True),
    ("unit", 0), ("formula", "x" * 4001),
    ("source_observed_at", "2026-10-03T12:00:00"),
    ("reference", "https://person:password@catalog.example/term"),  # pragma: allowlist secret - synthetic rejected credential fixture
    ("reference", "https://catalog.example/term?token=secret"), ("unsupported", "value"),
])
def test_malformed_definition_is_refused_without_partial_import(adapter, field, value):
    config, payload, _ = adapter
    payload["assets"][0]["upstream_definition"][field] = value
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover(config)
    assert error.value.code == "adapter_response_invalid"
    snapshot = curate_source("external_adapter", config)
    assert snapshot["status"] == "error" and snapshot["concepts"] == []


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_semantic_definition_requires_explicit_protocol_12_negotiation(adapter, monkeypatch, version):
    config, payload, profile = adapter
    profile["protocol_version"] = payload["protocol_version"] = version
    monkeypatch.setenv("KNOWLEDGE_SOURCE_SEMANTICS", json.dumps(profile))
    with pytest.raises(ConnectorError) as error:
        external_adapter.discover(config)
    assert error.value.code == "adapter_response_invalid"


@pytest.mark.parametrize("case", ["duplicate_identity", "table_schema", "missing_version", "over_budget", "partial_listing"])
def test_definition_identity_schema_and_inventory_boundaries_fail_closed(adapter, case):
    config, payload, _ = adapter
    if case == "duplicate_identity":
        duplicate = deepcopy(payload["assets"][0])
        duplicate["pointer"] = "definitions/another-pointer"
        payload["assets"].append(duplicate)
    elif case == "table_schema":
        payload["assets"][0]["schema"] = [{"name": "invented", "data_type": "text"}]
    elif case == "missing_version":
        payload["assets"][0]["upstream_definition"].pop("version")
    elif case == "over_budget":
        payload["assets"] = [_asset(f"business-{number}") for number in range(101)]
    else:
        payload["listing_complete"] = False
    snapshot = curate_source("external_adapter", config)
    assert snapshot["status"] == "error" and snapshot["assets"] == [] and snapshot["concepts"] == []
    expected = "asset_limit_exceeded" if case == "over_budget" else "adapter_listing_incomplete" if case == "partial_listing" else "adapter_response_invalid"
    assert snapshot["errors"][0]["code"] == expected
