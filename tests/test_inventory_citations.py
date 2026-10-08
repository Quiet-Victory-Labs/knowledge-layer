"""Actual inventory-to-assertion review effects; synthetic metadata only."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select, update

from knowledge_layer.adapters.catalog import inventory_records
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_citations import resolve_inventory_citation
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeStore,
    KnowledgeValidationError,
)
from knowledge_layer.task_context import revalidate_context, task_context


def commit_catalog(case, *, definition="Net revenue excludes refunds.", empty=False):
    store, source = case["store"], case["catalog"]
    expected = {"provider": "customer_catalog", "source_identity": "synthetic-finance-account",
                "namespace": "finance", "scope_id": "finance", "selectors": ["term:net-revenue"]}
    package = {"schema_version": "knowledge-catalog-export-v1", "scope": expected,
        "observation": {"exported_at": None, "source_observed_at": None, "native_revision": None,
                        "field_coverage": {"definitions": "complete", "policies": "unknown"}, "redaction": "unknown"},
        "listing_complete": True, "next_cursor": None, "record_count": 0 if empty else 1,
        "records": [] if empty else [{"native_id": "term:net-revenue", "native_type": "GlossaryTerm",
            "kind": "semantic_definition", "name": "Net revenue", "native_record": {
                "definition": definition, "unit": "USD", "native_approval": "approved",
                "opaque": [None, 0, False, ""], "policy_refs": ["unverified-native-policy"]}}]}
    capture = inventory_records(json.dumps(package).encode(), expected_scope=expected)
    reader = InventoryStore(store)
    run = reader.begin("demo", source, scope={"namespace": "finance", "selectors": expected["selectors"]},
        policy={"id": "metadata-only", "revision": None}, watermark=capture["watermark"],
        limits={"max_pages": 10, "max_entities": 100, "max_evidence": 100, "max_total_bytes": 1048576})
    run = reader.append("demo", source, run["id"], page_index=0, cursor=None, next_cursor=None,
        scope_digest=run["scope_digest"], policy_digest=run["policy_digest"], watermark_digest=run["watermark_digest"],
        entities=capture["entities"], evidence=capture["evidence"])
    run = reader.commit("demo", source, run["id"], completion={"listing_complete": True, "pages": 1,
        "entities": len(capture["entities"]), "evidence": len(capture["evidence"]), "manifest_digest": run["manifest_digest"]})
    if not empty:
        case["ref"] = {"connection_id": source, "inventory_id": run["id"], "manifest_digest": run["manifest_digest"],
                       "entity_id": capture["entities"][0]["id"], "evidence_id": capture["evidence"][0]["id"]}
    return run


def setup_case(tmp_path):
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    (warehouse / "orders.csv").write_text("order_id,net_amount\n1,12.50\n2,0\n3,\n")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.sqlite'}", local_files_root=tmp_path)
    target = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "warehouse"})
    catalog = store.create_connection("demo", kind="local_files", name="Catalog observations", config={"path": "catalog"})
    snapshot = store.refresh_connection("demo", target["id"])["snapshot"]
    asset = snapshot["assets"][0]
    handle = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == asset["id"]
                  and (item.get("details") or {}).get("field") == "net_amount")
    case = {"store": store, "target": target["id"], "catalog": catalog["id"], "asset": asset["id"], "handle": handle}
    commit_catalog(case)
    return case


@pytest.fixture
def citation_case(tmp_path):
    case = setup_case(tmp_path)
    try:
        yield case
    finally:
        case["store"].close()


def assertion(case, *, approve=False, refs=None, kind="field_description"):
    store, target = case["store"], case["target"]
    payload = ({"name": "Net revenue", "description": "Net revenue excludes refunded orders.",
                "measure_field": "net_amount", "aggregation": "sum", "unit": "USD"} if kind == "metric" else
               {"field": "net_amount", "description": "Net revenue on an order excludes refunded amounts."})
    proposal = store.submit_contract_assertion("demo", target, assertion_kind=kind, asset_id=case["asset"],
        payload=payload, evidence_ids=[case["handle"]], evidence_refs=[deepcopy(case["ref"])] if refs is None else refs,
        rationale="The local owner interprets this exact catalog observation for the orders dataset.",
        expected_snapshot_id=store.get_snapshot("demo", target)["id"])
    if approve:
        store.decide_proposal("demo", target, proposal["id"], decision="approve")
    return proposal


def publish(case):
    store, target = case["store"], case["target"]
    preview = store.publication_preview("demo", target)
    assert preview["eligible"], preview["blockers"]
    return store.publish_source("demo", target, expected_snapshot_id=preview["snapshot_id"],
        expected_snapshot_version=preview["snapshot_version"], rationale="The owner reviewed current local meaning and its exact evidence.")


def test_exact_catalog_citation_grounds_local_review_without_native_publication(citation_case):
    case, store = citation_case, citation_case["store"]
    state, proof = resolve_inventory_citation(store, "demo", case["ref"])
    assert state["entity"]["id"] == case["ref"]["entity_id"]
    assert proof["payload"]["native_record"]["opaque"] == [None, 0, False, ""]
    draft = assertion(case)
    contract = semantic_contract(store, tenant_id="demo", connection_id=case["target"])
    assert not contract["approved_assertions"] and not contract["cited_inventory_evidence"]
    store.decide_proposal("demo", case["target"], draft["id"], decision="approve")
    contract = semantic_contract(store, tenant_id="demo", connection_id=case["target"])
    assert contract["approved_assertions"][0]["id"] == draft["id"]
    citation = contract["cited_inventory_evidence"][0]
    assert citation["evidence_ref"] == case["ref"] and citation["meaning_certified"] is False
    assert citation["native_authority"] == "untrusted_evidence" and not citation["locally_executable"]
    assert "native_record" not in json.dumps(citation)
    assert store.get_snapshot("demo", case["catalog"]) is None
    publish(case)
    assert store.get_snapshot("demo", case["target"])["agent_ready"] is True
    assert store.get_connection("demo", case["catalog"])["agent_ready"] is False


@pytest.mark.parametrize("change", ["successor", "empty", "error"])
def test_changed_inventory_suspends_local_meaning_publication_and_task_receipt(citation_case, change):
    case, store = citation_case, citation_case["store"]
    assertion(case, approve=True, kind="metric")
    publish(case)
    context = task_context(store, tenant_id="demo", query="Net revenue", connection_ids=[case["target"]])
    assert revalidate_context(store, tenant_id="demo", receipt=context["receipt"])["status"] == "current"
    if change == "error":
        with store.engine.begin() as conn:
            conn.execute(update(store.connections).where(store.connections.c.id == case["catalog"]).values(status="error"))
    else:
        commit_catalog(case, definition="Different meaning after a source change.", empty=change == "empty")
    snapshot = store.get_snapshot("demo", case["target"])
    assert snapshot["agent_ready"] is False
    assert any(p.get("decision") == "approve" and p.get("effective_on_current_snapshot") is False
               for p in snapshot["proposals"])
    contract = semantic_contract(store, tenant_id="demo", connection_id=case["target"])
    assert not contract["metrics"] and not contract["cited_inventory_evidence"]
    with pytest.raises(KnowledgeConflict, match="Context"):
        revalidate_context(store, tenant_id="demo", receipt=context["receipt"])


@pytest.mark.parametrize("key,value", [("manifest_digest", "f" * 64), ("inventory_id", "missing"),
    ("entity_id", "missing"), ("evidence_id", "missing"), ("connection_id", "missing")])
def test_wrong_or_unrelated_pins_refuse_before_draft_insertion(citation_case, key, value):
    case, store = citation_case, citation_case["store"]
    before = store.engine.connect()
    with before as conn:
        count = conn.execute(select(func.count()).select_from(store.contract_proposals)).scalar_one()
    with pytest.raises(KnowledgeError):
        assertion(case, refs=[case["ref"] | {key: value}])
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.contract_proposals)).scalar_one() == count


@pytest.mark.parametrize("change", [{"snapshot_id": "invented"}, {"manifest_digest": None},
    {"inventory_id": False}, {"entity_id": ""}, {"evidence_id": []}])
def test_malformed_inventory_handles_never_fall_through_to_snapshot_refs(citation_case, change):
    with pytest.raises(KnowledgeValidationError):
        assertion(citation_case, refs=[citation_case["ref"] | change])


def test_duplicate_and_self_citations_refuse(citation_case):
    case = citation_case
    with pytest.raises(KnowledgeValidationError):
        assertion(case, refs=[case["ref"], case["ref"]])
    with pytest.raises(KnowledgeValidationError):
        assertion(case, refs=[case["ref"] | {"connection_id": case["target"]}])


def test_inventory_cannot_establish_a_cross_source_join(citation_case):
    with pytest.raises(KnowledgeValidationError, match="cannot establish a cross-source join"):
        assertion(citation_case, kind="cross_source_join")


def test_successor_during_submission_recheck_leaves_no_draft(citation_case, monkeypatch):
    case, store = citation_case, citation_case["store"]
    original = store._require_proposal_capacity

    def change(*args, **kwargs):
        commit_catalog(case, definition="Changed while the target writer waited.")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_require_proposal_capacity", change)
    with pytest.raises(KnowledgeConflict):
        assertion(case)
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.contract_proposals)).scalar_one() == 0


def test_dependency_withdrawal_after_review_lock_writes_no_decision(citation_case, monkeypatch):
    case, store = citation_case, citation_case["store"]
    proposal = assertion(case)
    original = store._lock_source

    def withdraw(conn, tenant, cid):
        result = original(conn, tenant, cid)
        if cid == case["target"]:
            with store.engine.begin() as writer:
                writer.execute(update(store.connections).where(store.connections.c.id == case["catalog"]).values(status="error"))
        return result

    monkeypatch.setattr(store, "_lock_source", withdraw)
    with pytest.raises(KnowledgeConflict):
        store.decide_proposal("demo", case["target"], proposal["id"], decision="approve")
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.decisions)).scalar_one() == 0


def test_resolver_rechecks_after_payload_selection(citation_case, monkeypatch):
    case, store = citation_case, citation_case["store"]
    original = InventoryStore.select

    def withdraw(*args, **kwargs):
        packet = original(*args, **kwargs)
        commit_catalog(case, empty=True)
        return packet

    monkeypatch.setattr(InventoryStore, "select", withdraw)
    with pytest.raises(KnowledgeConflict):
        resolve_inventory_citation(store, "demo", deepcopy(case["ref"]))


def test_contract_refuses_withdrawal_during_citation_assembly(citation_case, monkeypatch):
    from knowledge_layer import federated

    case, store = citation_case, citation_case["store"]
    assertion(case, approve=True, kind="metric")
    publish(case)
    original = federated.cited_inventory_evidence

    def withdraw(*args, **kwargs):
        result = original(*args, **kwargs)
        commit_catalog(case, empty=True)
        return result

    monkeypatch.setattr(federated, "cited_inventory_evidence", withdraw)
    with pytest.raises(KnowledgeConflict, match="during assembly"):
        semantic_contract(store, tenant_id="demo", connection_id=case["target"])


def test_task_packet_refuses_withdrawal_during_term_assembly(citation_case, monkeypatch):
    case, store = citation_case, citation_case["store"]
    assertion(case, approve=True, kind="metric")
    publish(case)
    original = store.get_context

    def withdraw(*args, **kwargs):
        result = original(*args, **kwargs)
        commit_catalog(case, empty=True)
        return result

    monkeypatch.setattr(store, "get_context", withdraw)
    with pytest.raises(KnowledgeConflict, match="during assembly"):
        task_context(store, tenant_id="demo", query="Net revenue", connection_ids=[case["target"]])


def test_compact_graph_and_rdf_keep_inventory_pins_without_snapshot_invention(citation_case):
    from rdflib import OWL, RDF

    from knowledge_layer.federated import federated_evidence_graph
    from knowledge_layer.mcp import _compact_federated_graph
    from knowledge_layer.ontology import KL, project_snapshot

    case, store = citation_case, citation_case["store"]
    assertion(case, approve=True)
    graph = _compact_federated_graph(federated_evidence_graph(store, tenant_id="demo", connection_id=case["target"]))
    native = [item for item in graph["nodes"] if item["kind"].startswith("inventory_")]
    assert len(native) == 2
    assert all(item["manifest_digest"] == case["ref"]["manifest_digest"] and "snapshot_id" not in item for item in native)
    assert next(item for item in native if item["kind"] == "inventory_evidence")["evidence_ref"] == case["ref"]
    rdf = project_snapshot(store.get_snapshot("demo", case["target"]), tenant_id="demo", connection_id=case["target"])
    citations = list(rdf.subjects(RDF.type, KL.InventoryEvidenceCitation))
    assert len(citations) == 1
    assert str(rdf.value(citations[0], KL.manifestDigest)) == case["ref"]["manifest_digest"]
    assert str(rdf.value(citations[0], KL.sourceTextAuthority)) == "untrusted_evidence"
    assert rdf.value(citations[0], KL.sourceSnapshot) is None
    assert not list(rdf.triples((None, OWL.sameAs, None)))


def test_shacl_accepts_real_inventory_citation_before_and_after_review(citation_case):
    pytest.importorskip("pyshacl")
    from knowledge_layer.ontology import project_snapshot
    from knowledge_layer.rdf_validation import validate_graph

    case, store = citation_case, citation_case["store"]
    proposal = assertion(case)
    for approved in [False, True]:
        if approved:
            store.decide_proposal("demo", case["target"], proposal["id"], decision="approve")
        snapshot = store.get_snapshot("demo", case["target"])
        report = validate_graph(project_snapshot(snapshot, tenant_id="demo", connection_id=case["target"]))
        assert report["conforms"] is True, report
        assert snapshot["agent_ready"] is False


def test_cached_citations_recheck_current_head_and_do_not_share_mutable_payload(citation_case, monkeypatch):
    from knowledge_layer.inventory_citations import inventory_citation_operation

    case, store = citation_case, citation_case["store"]
    ref, calls = deepcopy(case["ref"]), []
    original = InventoryStore.select

    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(InventoryStore, "select", count)
    with inventory_citation_operation():
        first = resolve_inventory_citation(store, "demo", ref)
        first[1]["payload"]["native_record"]["opaque"][0] = "changed by caller"
        for _ in range(10):
            assert resolve_inventory_citation(store, "demo", ref)[1]["payload"]["native_record"]["opaque"][0] is None
        assert len(calls) == 1
        commit_catalog(case, empty=True)
        with pytest.raises(KnowledgeConflict):
            resolve_inventory_citation(store, "demo", ref)
    assert len(calls) == 1


def test_budget_exhaustion_refuses_whole_currentness_operation(citation_case, monkeypatch):
    from knowledge_layer import inventory_citations
    from knowledge_layer.cross_source import dependency_projection

    case, store = citation_case, citation_case["store"]
    monkeypatch.setattr(inventory_citations, "MAX_CITATION_CHECKS", 3)
    calls, original = [], InventoryStore.select

    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(InventoryStore, "select", count)
    with dependency_projection(), pytest.raises(inventory_citations.InventoryCitationBudgetExceeded):
        for _ in range(4):
            assert store._external_evidence_current("demo", [case["ref"]])
    assert len(calls) == 1
    # The cache and budget never escape the failed operation.
    assert store._external_evidence_current("demo", [case["ref"]])
    assert len(calls) == 2


def test_distinct_record_budget_caps_full_selections(citation_case, monkeypatch):
    from knowledge_layer import inventory_citations

    case, store = citation_case, citation_case["store"]
    reader = InventoryStore(store)
    entities = [{"id": f"term:{i}", "kind": "semantic_definition", "evidence_ids": [f"proof:{i}"],
                 "payload": {"definition": f"Actual synthetic definition {i}"}} for i in range(3)]
    proofs = [{"id": f"proof:{i}", "kind": "native_observation", "payload": {"native_revision": None}} for i in range(3)]
    run = reader.begin("demo", case["catalog"], scope={"namespace": "budget", "selectors": [item["id"] for item in entities]},
        policy={"id": "metadata-only", "revision": None}, watermark={"kind": "unknown", "value": None},
        limits={"max_pages": 1, "max_entities": 3, "max_evidence": 3, "max_total_bytes": 16384})
    run = reader.append("demo", case["catalog"], run["id"], page_index=0, cursor=None, next_cursor=None,
        scope_digest=run["scope_digest"], policy_digest=run["policy_digest"], watermark_digest=run["watermark_digest"],
        entities=entities, evidence=proofs)
    run = reader.commit("demo", case["catalog"], run["id"], completion={"listing_complete": True,
        "pages": 1, "entities": 3, "evidence": 3, "manifest_digest": run["manifest_digest"]})
    calls, original = [], InventoryStore.select

    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(InventoryStore, "select", count)
    monkeypatch.setattr(inventory_citations, "MAX_CITATION_SELECTIONS", 2)
    with inventory_citations.inventory_citation_operation(), pytest.raises(inventory_citations.InventoryCitationBudgetExceeded):
        for index in range(3):
            resolve_inventory_citation(store, "demo", {"connection_id": case["catalog"], "inventory_id": run["id"],
                "manifest_digest": run["manifest_digest"], "entity_id": f"term:{index}", "evidence_id": f"proof:{index}"})
    assert len(calls) == 2


def test_compact_mcp_proposal_and_http_contract_deliver_exact_provenance(citation_case):
    import asyncio

    from fastapi.testclient import TestClient

    from knowledge_layer.agent_api import build_agent_api
    from knowledge_layer.quick_mcp import build_quick_mcp_server

    case, store = citation_case, citation_case["store"]

    async def exercise():
        server = build_quick_mcp_server(store)
        result = await server.call_tool("propose_contract", {"connection_id": case["target"],
            "assertion_kind": "field_description", "asset_id": case["asset"],
            "payload": {"field": "net_amount", "description": "Net revenue on an order excludes refunded amounts."},
            "evidence_ids": [case["handle"]], "evidence_refs": [case["ref"]],
            "expected_snapshot_id": store.get_snapshot("demo", case["target"])["id"],
            "rationale": "The owner interprets the exact observed catalog record for this field."})
        assert result.structured_content["approval_required"] is True
        proposal = result.structured_content["proposal"]
        assert proposal["evidence_refs"] == [case["ref"]]
        store.decide_proposal("demo", case["target"], proposal["id"], decision="approve")
        contract = (await server.call_tool("get_contract", {
            "connection_id": case["target"], "asset_id": case["asset"]})).structured_content
        assert contract["cited_inventory_evidence"][0]["evidence_ref"] == case["ref"]
        assert "native_record" not in json.dumps(contract["cited_inventory_evidence"])

    asyncio.run(exercise())
    with TestClient(build_agent_api(store)) as client:
        response = client.get(f"/sources/{case['target']}/contract")
        assert response.status_code == 200
        assert response.json()["cited_inventory_evidence"][0]["evidence_ref"] == case["ref"]
        commit_catalog(case, empty=True)
        response = client.get(f"/sources/{case['target']}/contract")
        assert response.status_code == 200
        assert response.json()["cited_inventory_evidence"] == response.json()["approved_assertions"] == []


def test_opaque_separator_ids_preserve_distinct_rdf_citations(citation_case):
    from rdflib import RDF

    from knowledge_layer.ontology import KL, project_snapshot

    case, store = citation_case, citation_case["store"]
    entities = [{"id": entity, "kind": "semantic_definition", "evidence_ids": [proof], "payload": {}}
                for entity, proof in (("a:b", "c"), ("a", "b:c"))]
    proofs = [{"id": proof, "kind": "native_observation", "payload": {}} for proof in ("c", "b:c")]
    reader = InventoryStore(store)
    run = reader.begin("demo", case["catalog"], scope={"namespace": "opaque", "selectors": ["a", "a:b"]},
        policy={"id": "metadata-only", "revision": None}, watermark={"kind": "unknown", "value": None},
        limits={"max_pages": 1, "max_entities": 2, "max_evidence": 2, "max_total_bytes": 16384})
    run = reader.append("demo", case["catalog"], run["id"], page_index=0, cursor=None, next_cursor=None,
        scope_digest=run["scope_digest"], policy_digest=run["policy_digest"], watermark_digest=run["watermark_digest"],
        entities=entities, evidence=proofs)
    run = reader.commit("demo", case["catalog"], run["id"], completion={"listing_complete": True,
        "pages": 1, "entities": 2, "evidence": 2, "manifest_digest": run["manifest_digest"]})
    refs = [{"connection_id": case["catalog"], "inventory_id": run["id"], "manifest_digest": run["manifest_digest"],
             "entity_id": entity["id"], "evidence_id": entity["evidence_ids"][0]} for entity in entities]
    assertion(case, refs=refs, approve=True)
    graph = project_snapshot(store.get_snapshot("demo", case["target"]), tenant_id="demo", connection_id=case["target"])
    citations = list(graph.subjects(RDF.type, KL.InventoryEvidenceCitation))
    assert len(citations) == 2
    assert all(len(list(graph.objects(node, KL.sourceEntity))) == len(list(graph.objects(node, KL.evidence))) == 1
               for node in citations)
