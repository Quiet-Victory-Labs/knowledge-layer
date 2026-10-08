"""The agent contract loop keeps source and document evidence attached to drafts."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import RDF, RDFS

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.contracts import (
    list_contract_work,
    ossie_preview,
    search_document_evidence,
    search_source_evidence,
    semantic_contract,
)
from knowledge_layer.federated import federated_evidence_graph
from knowledge_layer.gateway import create_gateway
from knowledge_layer.ontology import KL, project_snapshot
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


def test_approved_grain_becomes_stale_when_field_disappears_even_with_same_evidence(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    source_id = source["id"]
    base = {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-orders", "name": "Orders", "asset_type": "table",
                    "schema": [{"name": "order_id", "data_type": "text"}]}],
        "concepts": [], "relationships": [], "metrics": [], "proposals": [],
        "evidence": [{"id": "same-evidence", "asset_id": "asset-orders",
                      "evidence_type": "asset_metadata", "summary": "Orders schema"}],
        "errors": [],
    }
    first = store.save_snapshot("demo", source_id, {**base, "snapshot_id": "first"})
    proposal = store.submit_contract_assertion(
        "demo", source_id, assertion_kind="grain", asset_id="asset-orders",
        payload={"description": "One row per order", "key_fields": ["order_id"]},
        evidence_ids=["same-evidence"], rationale="The key identifies an order in this source.",
        expected_snapshot_id=first["id"],
    )
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve")
    assert store.get_snapshot("demo", source_id)["assets"][0]["grain_contract"]["status"] == "approved"
    changed = {**base, "assets": [{**base["assets"][0],
                                   "schema": [{"name": "new_order_key", "data_type": "text"}]}]}
    store.save_snapshot("demo", source_id, {**changed, "snapshot_id": "second"})
    current = store.get_snapshot("demo", source_id)
    assert current["proposals"][0]["effective_on_current_snapshot"] is False
    assert "grain_contract" not in current["assets"][0]
    assert "no_current_owner_review" in store.publication_preview("demo", source_id)["blockers"]
    store.close()


def test_document_grounded_field_contract_and_review(tmp_path):
    (tmp_path / "work_orders.csv").write_text(
        "work_order_id,labor_hours,client_id\nWO-1,4,CL-1\nWO-2,2,CL-2\n", encoding="utf-8"
    )
    (tmp_path / "clients.csv").write_text(
        "client_id,name\nCL-1,Acme\nCL-2,Beacon\n", encoding="utf-8"
    )
    (tmp_path / "policy.md").write_text(
        "# Field service policy\n\n## Billable hours\n\nLabor hours are recorded on one work order. Travel is separate.\n",
        encoding="utf-8",
    )
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    conn = store.create_connection("demo", kind="local_files", name="Field service", config={
        "path": ".", "retain_semantic_samples": True,
    })
    snapshot = store.refresh_connection("demo", conn["id"])["snapshot"]
    assert snapshot["profiling"]["join_probe_count"] > 0
    assert "CL-1" not in json.dumps(snapshot)
    work = list_contract_work(store, tenant_id="demo", connection_id=conn["id"], limit=10)
    assert any(item["kind"] == "dataset_description" for item in work["work"])
    assert any(item["kind"] == "grain" for item in work["work"])
    all_work = [*work["work"]]
    for offset in range(10, work["total"], 10):
        all_work.extend(list_contract_work(store, tenant_id="demo", connection_id=conn["id"], limit=10, offset=offset)["work"])
    assert any(item["kind"] == "join_rule" and item["candidate"].get("join_probe") for item in all_work)
    hours = next(item for item in all_work if item["kind"] == "field_description" and item["candidate"]["field"] == "labor_hours")
    assert len(hours["evidence_ids"]) >= 2
    docs = search_source_evidence(store, tenant_id="demo", connection_id=conn["id"], query="billable hours")
    assert any(item["evidence_type"] == "document_derived_term" for item in docs["matches"])
    proposal = store.submit_contract_assertion(
        "demo", conn["id"], assertion_kind="field_description", asset_id=hours["asset_id"],
        payload={"field": "labor_hours", "description": "Technician labor hours recorded on one work order; travel is separate."},
        evidence_ids=hours["evidence_ids"], rationale="The source field and field service policy support this scoped description.",
        expected_snapshot_id=snapshot["id"],
    )
    assert proposal["status"] == "pending_review"
    contract = semantic_contract(store, tenant_id="demo", connection_id=conn["id"])
    assert len(contract["pending_assertions"]) == 1
    assert contract["approved_assertions"] == []
    store.decide_proposal("demo", conn["id"], proposal["id"], decision="approve", rationale="Owner confirms policy scope")
    contract = semantic_contract(store, tenant_id="demo", connection_id=conn["id"])
    assert contract["approved_assertions"][0]["payload"]["field"] == "labor_hours"
    assert contract["pending_assertions"] == []
    dataset = next(item for item in contract["datasets"] if item["name"] == "work_orders.csv")
    field = next(item for item in dataset["fields"] if item["name"] == "labor_hours")
    assert field["description_status"] == "approved"
    graph = project_snapshot(store.get_snapshot("demo", conn["id"]), tenant_id="demo", connection_id=conn["id"])
    assert any(True for _ in graph.subjects(RDF.type, KL.Field))
    assert any(str(value) == field["description"] for value in graph.objects(None, RDFS.comment))
    assert contract["agent_ready"] is False
    (tmp_path / "work_orders.csv").write_text(
        "work_order_id,labor_hours,client_id\nWO-1,5,CL-1\nWO-2,2,CL-2\n", encoding="utf-8"
    )
    store.refresh_connection("demo", conn["id"])
    contract = semantic_contract(store, tenant_id="demo", connection_id=conn["id"])
    dataset = next(item for item in contract["datasets"] if item["name"] == "work_orders.csv")
    field = next(item for item in dataset["fields"] if item["name"] == "labor_hours")
    assert "description" not in field
    assert contract["approved_assertions"] == []
    assert len(contract["stale_assertions"]) == 1
    store.close()


def test_document_only_connection_builds_reviewable_glossary_node(tmp_path):
    (tmp_path / "policy.md").write_text(
        "# Service glossary\n\n## Billable hours\n\nTechnician labor booked to a work order; travel is separate.\n",
        encoding="utf-8",
    )
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Service policy", config={
        "path": ".", "retain_semantic_samples": True,
    })
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    concept = next(item for item in snapshot["concepts"] if item.get("name") == "Billable hours")
    assert concept["definition"] is None
    proposal = store.submit_proposal(
        "demo", source["id"], target_concept_id=concept["id"],
        proposed_definition="Technician labor booked to one work order; travel is separate.",
        evidence_ids=concept["evidence_ids"], rationale="The policy defines the term under its heading.",
        expected_snapshot_id=snapshot["id"],
    )
    assert proposal["status"] == "pending_review"
    store.decide_proposal("demo", source["id"], proposal["id"], decision="approve",
                          rationale="The policy owner confirmed the definition.")
    contract = semantic_contract(store, tenant_id="demo", connection_id=source["id"])
    glossary = next(item for item in contract["glossary"] if item["term"] == "Billable hours")
    assert glossary["definition_status"] == "approved"
    graph = store.get_graph("demo", source["id"], focus_id=concept["id"], depth=2)
    assert graph["nodes"][0]["description"] == glossary["definition"]
    assert any(edge["predicate"] == "supported_by" for edge in graph["edges"])
    store.close()


def test_contract_rejects_unsupported_join_and_stale_snapshot(tmp_path):
    (tmp_path / "left.csv").write_text("id,right_id\n1,2\n", encoding="utf-8")
    (tmp_path / "right.csv").write_text("id\n2\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    conn = store.create_connection("demo", kind="local_files", name="Join check", config={"path": "."})
    snapshot = store.refresh_connection("demo", conn["id"])["snapshot"]
    assets = {item["name"]: item for item in snapshot["assets"]}
    left, right = assets["left.csv"], assets["right.csv"]
    evidence = next(item["id"] for item in snapshot["evidence"] if item.get("asset_id") == left["id"])
    payload = {"to_asset_id": right["id"], "from_columns": ["right_id"], "to_columns": ["id"],
               "cardinality": "many_to_one", "caveat": "Historical identity and key overlap remain unverified."}
    with pytest.raises(KnowledgeValidationError, match="both source assets"):
        store.submit_contract_assertion(
            "demo", conn["id"], assertion_kind="join_rule", asset_id=left["id"], payload=payload,
            evidence_ids=[evidence], rationale="The source field names suggest a possible join.",
            expected_snapshot_id=snapshot["id"],
        )
    with pytest.raises(KnowledgeConflict, match="Source changed"):
        store.submit_contract_assertion(
            "demo", conn["id"], assertion_kind="dataset_description", asset_id=left["id"],
            payload={"description": "A table of source records."}, evidence_ids=[evidence],
            rationale="The source inventory identifies this table.", expected_snapshot_id="old",
        )
    with pytest.raises(KnowledgeValidationError, match="queryable SQL source"):
        store.submit_contract_assertion(
            "demo", conn["id"], assertion_kind="query_example", asset_id=left["id"],
            payload={"question": "Show all records", "dialect": "ANSI_SQL", "sql": "SELECT * FROM left",
                     "expected_grain": "One row per left record"},
            evidence_ids=[evidence], rationale="The sample query uses the discovered table.",
            expected_snapshot_id=snapshot["id"],
        )
    store.close()


def test_contract_agent_api_is_protected_and_reviewable(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    store.refresh_connection("demo", connection["id"])
    gateway = create_gateway(store=store, agent_token="test-only")
    path = f"/agent/api/contract-work?connection_id={connection['id']}"
    with TestClient(gateway) as client:
        assert client.get(path).status_code == 401
        headers = {"Authorization": "Bearer test-only"}
        packet = client.get(path, headers=headers).json()["work"][0]
        assert packet["kind"] == "dataset_description"
        proposal = client.post("/agent/api/contract-assertions", headers=headers, json={
            "connection_id": connection["id"], "assertion_kind": "dataset_description",
            "asset_id": packet["asset_id"], "payload": {"description": "One record per customer order."},
            "evidence_ids": packet["evidence_ids"],
            "rationale": "The source filename and fields support this draft description.",
            "expected_snapshot_id": packet["snapshot_id"],
        })
        assert proposal.status_code == 202
        contract = client.get(f"/agent/api/sources/{connection['id']}/contract", headers=headers)
        assert contract.status_code == 200
        assert len(contract.json()["pending_assertions"]) == 1
        assert contract.json()["approved_assertions"] == []
    store.close()


def test_zero_overlap_evidence_survives_without_inferred_join_and_reaches_agent_packets(tmp_path):
    (tmp_path / "schedule_rows.jsonl").write_text(
        '{"player_id":"private-schedule-player-4071"}\n', encoding="utf-8",
    )
    vendor_rows = tmp_path / "vendor_rows.jsonl"
    vendor_rows.write_text('{"player_id":"private-vendor-player-6923"}\n', encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    connection = store.create_connection("demo", kind="local_files", name="Company excerpts", config={"path": "."})
    snapshot = store.refresh_connection("demo", connection["id"])["snapshot"]
    probes = [item for item in snapshot["evidence"] if item["evidence_type"] == "bounded_join_probe"]
    assert len(probes) == 2
    assert all(item["details"]["matched_source_rows"] == 0 for item in probes)
    assert all(item["details"]["source_nonnull_rows_probed"] == 1 for item in probes)
    assert all(item["details"]["raw_values_retained"] is False for item in probes)
    assert all(item["trust_state"] == "observed_profile" for item in probes)
    assert snapshot["relationships"] == []
    assert semantic_contract(store, tenant_id="demo", connection_id=connection["id"])["joins"] == []
    assert snapshot["meaning_certified"] is False
    assert snapshot["agent_ready"] is False
    probe_ids = {item["id"] for item in probes}
    again = store.refresh_connection("demo", connection["id"])["snapshot"]
    assert {item["id"] for item in again["evidence"] if item["evidence_type"] == "bounded_join_probe"} == probe_ids
    assert again["snapshot_id"] == snapshot["snapshot_id"]

    gateway = create_gateway(store=store, agent_token="test-only")
    headers = {"Authorization": "Bearer test-only"}
    with TestClient(gateway) as client:
        search = client.get(
            f"/agent/api/sources/{connection['id']}/evidence/search",
            params={"q": "player_id", "limit": 10}, headers=headers,
        )
        assert search.status_code == 200
        assert {item["id"] for item in search.json()["matches"]
                if item["evidence_type"] == "bounded_join_probe"} == probe_ids
        packets = client.get("/agent/api/curation-work",
                             params={"connection_id": connection["id"], "limit": 10}, headers=headers)
        assert packets.status_code == 200
        assert probe_ids <= {item["id"] for packet in packets.json()["work"] for item in packet["evidence"]}
        assert not any(packet["kind"] == "review_relationship" for packet in packets.json()["work"])
        assert "private-schedule-player-4071" not in json.dumps(packets.json())
        assert "private-vendor-player-6923" not in json.dumps(search.json())

    # Same aggregate counts over changed source records are new evidence.
    vendor_rows.write_text('{"player_id":"private-vendor-player-9812"}\n', encoding="utf-8")
    changed = store.refresh_connection("demo", connection["id"])["snapshot"]
    changed_probes = [item for item in changed["evidence"] if item["evidence_type"] == "bounded_join_probe"]
    assert {item["id"] for item in changed_probes}.isdisjoint(probe_ids)
    assert all(item["details"]["matched_source_rows"] == 0 for item in changed_probes)
    assert "private-vendor-player-9812" not in json.dumps(changed)
    store.close()


def test_unmeasured_metadata_does_not_manufacture_zero_overlap_evidence():
    from knowledge_layer.curation import _curate

    discovery = {
        "source": {"id": "source-metadata", "kind": "external_adapter", "name": "Metadata only"},
        "assets": [{"id": "asset-left", "name": "left", "asset_type": "table", "source_pointer": "left",
                    "revision": "schema-only", "schema": [{"name": "player_id", "data_type": "text", "nullable": True}],
                    "profile": {"row_count": None, "profile_rows": 0, "metadata_only": True}}],
        "capabilities": {"metadata": True, "safe_profiling": False}, "errors": [],
    }
    snapshot = _curate(discovery, None)
    assert not any(item["evidence_type"] == "bounded_join_probe" for item in snapshot["evidence"])
    assert snapshot["relationships"] == []


def test_approved_sql_contract_projects_to_pinned_ossie_schema():
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    conn = store.create_connection("demo", kind="sql_catalog", name="Order warehouse", config={
        "provider": "postgresql", "secret_ref": "KNOWLEDGE_SOURCE_TEST",
    })
    snapshot = store.save_snapshot("demo", conn["id"], {
        "snapshot_id": "sql-snapshot-1", "status": "discovery_complete", "discovery_complete": True,
        "source": {"id": "source-sql", "kind": "sql_catalog"},
        "assets": [{"id": "asset-orders", "name": "orders", "asset_type": "table",
                    "source_pointer": "sql/postgresql/public/orders", "status": "observed",
                    "schema": [{"name": "order_id", "data_type": "text", "nullable": False},
                               {"name": "amount", "data_type": "number", "nullable": False}]}],
        "evidence": [{"id": "evidence-orders", "asset_id": "asset-orders",
                      "source_pointer": "sql/postgresql/public/orders", "summary": "Order table schema"}],
        "concepts": [], "relationships": [], "metrics": [], "proposals": [],
    })
    proposal = store.submit_contract_assertion(
        "demo", conn["id"], assertion_kind="grain", asset_id="asset-orders",
        payload={"description": "One row per order", "key_fields": ["order_id"]},
        evidence_ids=["evidence-orders"], rationale="The source declares a candidate order key.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", conn["id"], proposal["id"], decision="approve",
                          rationale="Data owner confirms order grain")
    export = ossie_preview(store, tenant_id="demo", connection_id=conn["id"])
    assert export["status"] == "draft_valid", export["schema_errors"]
    assert export["model"]["datasets"][0]["primary_key"] == ["order_id"]
    assert export["model"]["datasets"][0]["source"] == "public.orders"
    assert export["meaning_certified"] is False
    store.close()


def test_separate_document_source_can_ground_and_invalidate_field(tmp_path):
    (tmp_path / "warehouse").mkdir()
    (tmp_path / "policy").mkdir()
    (tmp_path / "warehouse" / "work_orders.csv").write_text(
        "work_order_id,labor_hours\nWO-1,4\n", encoding="utf-8"
    )
    policy_path = tmp_path / "policy" / "field_policy.md"
    policy_path.write_text("# Policy\n\n## Billable hours\n\nLabor hours belong to a work order. Travel is separate.\n",
                           encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    warehouse = store.create_connection("demo", kind="local_files", name="Warehouse", config={"path": "warehouse"})
    docs = store.create_connection("demo", kind="local_files", name="Policy", config={
        "path": "policy", "retain_semantic_samples": True,
    })
    source_snapshot = store.refresh_connection("demo", warehouse["id"])["snapshot"]
    store.refresh_connection("demo", docs["id"])
    hit = next(item for item in search_document_evidence(store, tenant_id="demo", query="billable hours")["matches"]
               if item["connection_id"] == docs["id"])
    asset = source_snapshot["assets"][0]
    local_field_evidence = next(item["id"] for item in source_snapshot["evidence"]
                                if item.get("asset_id") == asset["id"]
                                and (item.get("details") or {}).get("field") == "labor_hours")
    proposal = store.submit_contract_assertion(
        "demo", warehouse["id"], assertion_kind="field_description", asset_id=asset["id"],
        payload={"field": "labor_hours", "description": "Labor hours on one work order, excluding travel."},
        evidence_ids=[local_field_evidence], evidence_refs=[hit["evidence_ref"]],
        rationale="The field exists in the warehouse and the policy defines its scope.",
        expected_snapshot_id=source_snapshot["id"],
    )
    pending_graph = federated_evidence_graph(store, tenant_id="demo", connection_id=warehouse["id"])
    assert pending_graph["linked_assertions"] == 1
    assert any(edge["predicate"] == "grounded_in" and edge["status"] == "pending_review"
               for edge in pending_graph["edges"])
    assert {node["connection_id"] for node in pending_graph["nodes"]} == {warehouse["id"], docs["id"]}
    with pytest.raises(KnowledgeNotFound):
        federated_evidence_graph(store, tenant_id="another-tenant", connection_id=warehouse["id"])
    platform = FastAPI()
    platform.include_router(create_knowledge_router(store=store))
    with TestClient(platform) as client:
        response = client.get(f"/api/knowledge/connections/{warehouse['id']}/federated-graph")
        assert response.status_code == 200
        assert response.json()["linked_assertions"] == 1
    with TestClient(create_gateway(store=store, agent_token="test-token")) as client:
        assert client.get("/agent/api/federated-graph", params={"connection_id": warehouse["id"]}).status_code == 401
        response = client.get("/agent/api/federated-graph", params={"connection_id": warehouse["id"]},
                              headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert response.json()["linked_assertions"] == 1
    store.decide_proposal("demo", warehouse["id"], proposal["id"], decision="approve",
                          rationale="Operations owner confirms this policy")
    approved_graph = federated_evidence_graph(store, tenant_id="demo", connection_id=warehouse["id"])
    assert any(edge["predicate"] == "grounded_in" and edge["status"] == "approved"
               for edge in approved_graph["edges"])
    assert any(node["kind"] == "concept" and node["connection_id"] == docs["id"]
               for node in approved_graph["nodes"])
    # A connector cannot publish itself. A steward pins the reviewed version.
    untrusted = store.save_snapshot("demo", warehouse["id"], {
        **source_snapshot, "snapshot_id": "fixture-ready-source", "status": "ready",
        "agent_ready": True, "meaning_certified": True, "curation_status": "ready",
    })
    assert untrusted["agent_ready"] is False
    preview = store.publication_preview("demo", warehouse["id"])
    assert preview["eligible"] is True
    store.publish_source(
        "demo", warehouse["id"], expected_snapshot_id=untrusted["id"],
        expected_snapshot_version=untrusted["version"],
        rationale="Operations owner accepts this evidence-bound source for agent use.",
    )
    assert store.get_connection("demo", warehouse["id"])["snapshot"]["agent_ready"] is True
    contract = semantic_contract(store, tenant_id="demo", connection_id=warehouse["id"])
    assert contract["datasets"][0]["fields"][1]["description_status"] == "approved"
    assert contract["approved_assertions"][0]["evidence_refs"] == [hit["evidence_ref"]]
    graph = store.get_graph("demo", warehouse["id"], focus_id=asset["id"], depth=2)
    approved_field = next(node for node in graph["nodes"] if node["kind"] == "field" and node["label"] == "labor_hours")
    assert approved_field["description"] == "Labor hours on one work order, excluding travel."
    assert any(edge["predicate"] == "has_field" and edge["object_id"] == approved_field["id"]
               for edge in graph["edges"])
    policy_path.write_text("# Policy\n\n## Billable hours\n\nTravel time may now be included.\n", encoding="utf-8")
    store.refresh_connection("demo", docs["id"])
    contract = semantic_contract(store, tenant_id="demo", connection_id=warehouse["id"])
    assert "description" not in contract["datasets"][0]["fields"][1]
    assert contract["approved_assertions"] == []
    assert len(contract["stale_assertions"]) == 1
    stale_status = store.get_connection("demo", warehouse["id"])
    assert stale_status["stale_assertion_count"] == 1
    assert stale_status["stage"] == "needs_review"
    assert stale_status["needs_attention_count"] >= 1
    assert stale_status["snapshot"]["agent_ready"] is False
    assert stale_status["snapshot"]["meaning_certified"] is False
    stale_graph = federated_evidence_graph(store, tenant_id="demo", connection_id=warehouse["id"])
    assert stale_graph["stale_citations"] == 1
    assert stale_graph["linked_assertions"] == 0
    assert not any(edge["predicate"] == "grounded_in" for edge in stale_graph["edges"])
    graph = store.get_graph("demo", warehouse["id"], focus_id=asset["id"], depth=2)
    assert not any(node["kind"] == "field" for node in graph["nodes"])
    store.close()


@pytest.fixture
def cited_sources(tmp_path):
    (tmp_path / "warehouse").mkdir()
    (tmp_path / "policy").mkdir()
    (tmp_path / "warehouse" / "work_orders.csv").write_text(
        "work_order_id,labor_hours\nWO-1,4\n", encoding="utf-8",
    )
    policy_path = tmp_path / "policy" / "policy.md"
    policy_path.write_text(
        "# Service policy\n\n## Billable hours\n\nLabor belongs to one work order; travel is separate.\n\n"
        "## Uncited retention\n\nThe policy owner maintains archived work orders.\n\n"
        "## Unreviewed allowance\n\nAn allowance needs a separate review.\n",
        encoding="utf-8",
    )
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    warehouse = store.create_connection("demo", kind="local_files", name="Warehouse", config={"path": "warehouse"})
    docs = store.create_connection("demo", kind="local_files", name="Policy", config={
        "path": "policy", "retain_semantic_samples": True,
    })
    source_snapshot = store.refresh_connection("demo", warehouse["id"])["snapshot"]
    doc_snapshot = store.refresh_connection("demo", docs["id"])["snapshot"]
    terms = {item["name"]: item for item in doc_snapshot["concepts"]}
    for name, definition in (
        ("Billable hours", "Chargeable technician effort excluding travel."),
        ("Uncited retention", "An independently reviewed archival definition."),
    ):
        doc_snapshot = store.get_snapshot("demo", docs["id"])
        proposal = store.submit_proposal(
            "demo", docs["id"], target_concept_id=terms[name]["id"],
            proposed_definition=definition, evidence_ids=terms[name]["evidence_ids"],
            rationale="The policy owner reviewed this exact glossary definition.",
            expected_snapshot_id=doc_snapshot["id"],
        )
        store.decide_proposal("demo", docs["id"], proposal["id"], decision="approve")
    doc_snapshot = store.get_snapshot("demo", docs["id"])
    asset = source_snapshot["assets"][0]
    evidence_id = next(item["id"] for item in source_snapshot["evidence"]
                       if item.get("asset_id") == asset["id"]
                       and (item.get("details") or {}).get("field") == "labor_hours")
    ref = {"connection_id": docs["id"], "snapshot_id": doc_snapshot["id"],
           "evidence_id": terms["Billable hours"]["evidence_ids"][0]}
    pending = store.submit_contract_assertion(
        "demo", warehouse["id"], assertion_kind="field_description", asset_id=asset["id"],
        payload={"field": "labor_hours", "description": "Labor on a work order, excluding travel."},
        evidence_ids=[evidence_id], evidence_refs=[ref],
        rationale="This current field is explained by the reviewed service policy.",
        expected_snapshot_id=source_snapshot["id"],
    )
    try:
        yield {"store": store, "warehouse": warehouse["id"], "docs": docs["id"],
               "asset": asset["id"], "source_snapshot": source_snapshot, "doc_snapshot": doc_snapshot,
               "terms": terms, "ref": ref, "proposal": pending, "evidence_id": evidence_id,
               "policy_path": policy_path}
    finally:
        store.close()


def test_source_scoped_cited_glossary_requires_both_reviews_and_preserves_provenance(cited_sources):
    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    pending = store.get_context("demo", term="Billable hours", connection_id=source_id)
    assert pending["matches"] == pending["cited_glossary"] == []
    assert semantic_contract(store, tenant_id="demo", connection_id=source_id)["cited_glossary"] == []
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    context = store.get_context("demo", term="Billable hours", connection_id=source_id)
    assert context["matches"] == []  # A document definition never becomes a local source concept.
    assert context["cited_glossary_total_matches"] == 1
    assert context["cited_glossary_more_available"] is False
    citation = context["cited_glossary"][0]
    assert citation["term"] == "Billable hours"
    assert citation["definition"] == "Chargeable technician effort excluding travel."
    assert citation["definition_status"] == "approved"
    assert citation["trust_state"] == "owner_reviewed"
    assert citation["concept_trust_state"] == "inferred"
    assert citation["connection_id"] == fixture["docs"]
    assert citation["snapshot_id"] == fixture["doc_snapshot"]["id"]
    assert citation["snapshot_version"] == fixture["doc_snapshot"]["version"]
    assert citation["evidence_ref"] == fixture["ref"]
    assert fixture["ref"]["evidence_id"] in citation["evidence_ids"]
    current_source = store.get_snapshot("demo", source_id)
    assert citation["source_assertion"] == {
        "connection_id": source_id, "snapshot_id": current_source["id"],
        "snapshot_version": current_source["version"],
        "assertion_id": fixture["proposal"]["id"], "assertion_kind": "field_description",
        "asset_id": fixture["asset"], "field": "labor_hours", "status": "approved",
        "effective_on_current_snapshot": True, "evidence_ids": [fixture["evidence_id"]],
    }
    definition_match = store.get_context("demo", term="chargeable technician effort", connection_id=source_id)
    assert definition_match["matches"] == definition_match["cited_glossary"] == []
    definition_candidate = next(item for item in definition_match["retrieval_candidates"]
                                if item["candidate_type"] == "cited_glossary")
    assert definition_candidate["match_basis"] == "text_phrase"
    assert definition_candidate["requires_confirmation"] is True
    assert definition_candidate["context"] == citation
    document_match = store.get_context("demo", term="chargeable technician effort", connection_id=fixture["docs"])
    assert document_match["matches"] == []
    document_candidate = next(item for item in document_match["retrieval_candidates"]
                              if item["candidate_type"] == "concept")
    assert document_candidate["context"]["concept"]["id"] == fixture["terms"]["Billable hours"]["id"]
    assert document_candidate["requires_confirmation"] is True
    assert document_match["cited_glossary"] == []
    # Another reviewed definition in the same document still needs its own source citation.
    assert store.get_context("demo", term="Uncited retention", connection_id=source_id)["cited_glossary"] == []
    contract = semantic_contract(store, tenant_id="demo", connection_id=source_id)
    assert contract["glossary"] == []
    assert contract["cited_glossary"] == [citation]
    assert contract["joins"] == []
    candidate_ref = {**fixture["ref"], "evidence_id": fixture["terms"]["Unreviewed allowance"]["evidence_ids"][0]}
    candidate = store.submit_contract_assertion(
        "demo", source_id, assertion_kind="field_description", asset_id=fixture["asset"],
        payload={"field": "labor_hours", "description": "Labor has a separately cited allowance."},
        evidence_ids=[fixture["evidence_id"]], evidence_refs=[candidate_ref],
        rationale="The source owner accepts this field assertion, while the glossary definition remains unreviewed.",
        expected_snapshot_id=current_source["id"],
    )
    store.decide_proposal("demo", source_id, candidate["id"], decision="approve")
    assert store.get_context("demo", term="Unreviewed allowance", connection_id=source_id)["cited_glossary"] == []
    contract = semantic_contract(store, tenant_id="demo", connection_id=source_id)
    assert [entry["term"] for entry in contract["cited_glossary"]] == ["Billable hours"]


def test_cited_glossary_survives_http_and_compact_serialization(cited_sources):
    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    headers = {"Authorization": "Bearer cited-test"}
    with TestClient(create_gateway(store=store, agent_token="cited-test")) as client:
        context = client.get("/agent/api/context", params={"term": "Billable hours", "connection_id": source_id}, headers=headers)
        assert context.status_code == 200
        entry = context.json()["cited_glossary"][0]
        assert entry["evidence_ref"] == fixture["ref"]
        assert entry["source_assertion"]["assertion_id"] == fixture["proposal"]["id"]
        contract = client.get(f"/agent/api/sources/{source_id}/contract", headers=headers)
        assert contract.status_code == 200
        assert contract.json()["cited_glossary"][0]["definition"] == "Chargeable technician effort excluding travel."

    async def compact_calls():
        server = build_quick_mcp_server(store)
        context = (await server.call_tool("resolve_context", {"term": "chargeable technician effort", "connection_id": source_id})).structured_content
        assert context["matches"] == context["cited_glossary"] == []
        candidate = next(item for item in context["retrieval_candidates"] if item["candidate_type"] == "cited_glossary")
        assert candidate["requires_confirmation"] is True
        assert candidate["context"]["evidence_ref"] == fixture["ref"]
        assert candidate["context"]["source_assertion"]["assertion_id"] == fixture["proposal"]["id"]
        contract = (await server.call_tool("get_contract", {"connection_id": source_id, "asset_id": fixture["asset"]})).structured_content
        assert contract["cited_glossary"][0]["definition_status"] == "approved"
        assert contract["cited_glossary"][0]["source_assertion"]["asset_id"] == fixture["asset"]

    asyncio.run(compact_calls())


def test_cited_glossary_disappears_when_document_snapshot_changes(cited_sources):
    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    assert store.get_context("demo", term="Billable hours", connection_id=source_id)["cited_glossary"]
    fixture["policy_path"].write_text("# Policy\n\n## Billable hours\n\nTravel can now be included.\n", encoding="utf-8")
    new_doc = store.refresh_connection("demo", fixture["docs"])["snapshot"]
    assert new_doc["id"] != fixture["ref"]["snapshot_id"]
    assert store.get_context("demo", term="Billable hours", connection_id=source_id)["cited_glossary"] == []
    assert semantic_contract(store, tenant_id="demo", connection_id=source_id)["cited_glossary"] == []


@pytest.mark.parametrize("revoked", ["docs", "warehouse"])
def test_cited_glossary_honors_each_source_permission_and_revocation(cited_sources, revoked):
    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    for cid in (source_id, fixture["docs"]):
        store.grant_source_access("demo", cid, principal_type="user", principal_id="reader", role="read")
    store.access_enforced = True
    reader = Principal("reader", frozenset(), frozenset())
    with principal_scope(reader):
        assert store.get_context("demo", term="Billable hours", connection_id=source_id)["cited_glossary"]
    store.access_enforced = False  # Offline operator changes source grants.
    store.revoke_source_access("demo", fixture[revoked], principal_type="user", principal_id="reader")
    store.access_enforced = True
    with principal_scope(reader):
        context = store.get_context("demo", term="Billable hours", connection_id=source_id)
        if revoked == "warehouse":
            assert context is None
            with pytest.raises(ValueError, match="not found"):
                semantic_contract(store, tenant_id="demo", connection_id=source_id)
        else:
            assert context["cited_glossary"] == []
            assert semantic_contract(store, tenant_id="demo", connection_id=source_id)["cited_glossary"] == []


def test_cited_glossary_reordered_phrase_is_only_a_provenance_pinned_retrieval_candidate(cited_sources):
    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    exact = store.get_context("demo", term="Billable hours", connection_id=source_id)["cited_glossary"][0]
    context = store.get_context("demo", term="hours billable", connection_id=source_id)
    assert context["matches"] == context["cited_glossary"] == []
    candidate = next(item for item in context["retrieval_candidates"] if item["candidate_type"] == "cited_glossary")
    assert candidate["result_kind"] == "retrieval_candidate"
    assert candidate["match_basis"] == "token_overlap"
    assert candidate["requires_confirmation"] is True
    assert candidate["context"] == exact
    assert candidate["context"]["evidence_ref"] == fixture["ref"]
    assert candidate["context"]["source_assertion"]["assertion_id"] == fixture["proposal"]["id"]


@pytest.mark.parametrize("invalidation", ["document_refresh", "document_permission_revocation"])
def test_cited_glossary_retrieval_candidate_disappears_with_its_authority(cited_sources, invalidation):
    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    for cid in (source_id, fixture["docs"]):
        store.grant_source_access("demo", cid, principal_type="user", principal_id="reader", role="read")
    reader = Principal("reader", frozenset(), frozenset())
    store.access_enforced = True
    with principal_scope(reader):
        initial = store.get_context("demo", term="hours billable", connection_id=source_id)
        assert any(item["candidate_type"] == "cited_glossary" for item in initial["retrieval_candidates"])
    store.access_enforced = False  # Offline operator refreshes sources or changes grants.
    if invalidation == "document_refresh":
        fixture["policy_path"].write_text("# Policy\n\n## Billable hours\n\nTravel may be included.\n", encoding="utf-8")
        store.refresh_connection("demo", fixture["docs"])
    else:
        store.revoke_source_access("demo", fixture["docs"], principal_type="user", principal_id="reader")
    store.access_enforced = True
    with principal_scope(reader):
        current = store.get_context("demo", term="hours billable", connection_id=source_id)
        assert current["matches"] == current["cited_glossary"] == []
        assert not any(item["candidate_type"] == "cited_glossary" for item in current["retrieval_candidates"])


@pytest.mark.parametrize("failure", ["adapter_failure", "incomplete_discovery"])
def test_failed_glossary_refresh_suspends_cited_publication_without_erasing_evidence(cited_sources, failure):
    from knowledge_layer.store import CurationFailed
    from knowledge_layer.task_context import revalidate_context, task_context

    fixture = cited_sources
    store, source_id = fixture["store"], fixture["warehouse"]
    store.decide_proposal("demo", source_id, fixture["proposal"]["id"], decision="approve")
    snapshot = store.get_snapshot("demo", source_id)
    store.publish_source("demo", source_id, expected_snapshot_id=snapshot["id"],
                         expected_snapshot_version=snapshot["version"],
                         rationale="Owner reviewed the field against the current policy glossary.")
    before = store.get_connection("demo", fixture["docs"])
    packet = task_context(store, tenant_id="demo", query="Billable hours", connection_ids=[source_id])
    assert packet["status"] == "context_available"
    assert store.get_snapshot("demo", source_id)["agent_ready"] is True

    def failed_curator(kind, config):
        if failure == "adapter_failure":
            raise RuntimeError("The document service is unavailable")
        return {"status": "error", "discovery_complete": False, "assets": [], "evidence": []}

    if failure == "adapter_failure":
        with pytest.raises(CurationFailed):
            store.refresh_connection("demo", fixture["docs"], curator=failed_curator)
    else:
        store.refresh_connection("demo", fixture["docs"], curator=failed_curator)
    retained = store.get_connection("demo", fixture["docs"])
    assert retained["status"] == "error"
    assert retained["snapshot"]["id"] == before["snapshot"]["id"]
    assert retained["last_complete_refresh_at"] == before["last_complete_refresh_at"]
    assert retained["snapshot"]["evidence"] == before["snapshot"]["evidence"]
    current = store.get_snapshot("demo", source_id)
    assert current["agent_ready"] is False
    assert next(item for item in current["proposals"] if item["id"] == fixture["proposal"]["id"])[
        "effective_on_current_snapshot"] is False
    context = store.get_context("demo", term="Billable hours", connection_id=source_id)
    assert context["cited_glossary"] == []
    assert semantic_contract(store, tenant_id="demo", connection_id=source_id)["cited_glossary"] == []
    with pytest.raises(KnowledgeConflict, match="changed"):
        revalidate_context(store, tenant_id="demo", receipt=packet["receipt"])
    assert task_context(store, tenant_id="demo", query="Billable hours", connection_ids=[source_id])["status"] == "blocked"


def test_metric_guidance_follows_effective_approval_and_current_schema_without_results():
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    source = store.create_connection("demo", kind="sql_catalog", name="Accounting metadata", config={
        "provider": "postgresql", "secret_ref": "KNOWLEDGE_SOURCE_TEST",
    })
    base = {
        "snapshot_id": "accounting-first", "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-runs", "name": "research_runs", "asset_type": "table",
                    "source_pointer": "sql/postgresql/agent/research_runs",
                    "schema": [{"name": "cost_actual_usd", "data_type": "number",
                                "native_type": "numeric(30,12)", "nullable": True}]}],
        "evidence": [{"id": "cost-schema", "asset_id": "asset-runs", "evidence_type": "column_schema",
                      "details": {"field": "cost_actual_usd"}, "summary": "Observed optional cost field"}],
        "concepts": [], "relationships": [], "metrics": [], "proposals": [],
    }
    try:
        first = store.save_snapshot("demo", source["id"], base)
        proposal = store.submit_contract_assertion(
            "demo", source["id"], assertion_kind="metric", asset_id="asset-runs",
            payload={"name": "Recorded cost", "description": "Sum known costs while preserving missing and zero values.",
                     "measure_field": "cost_actual_usd", "aggregation": "sum", "unit": "USD"},
            evidence_ids=["cost-schema"], rationale="The owner reviewed this optional source measure.",
            expected_snapshot_id=first["id"],
        )
        assert semantic_contract(store, tenant_id="demo", connection_id=source["id"])["metrics"] == []
        store.decide_proposal("demo", source["id"], proposal["id"], decision="approve")
        current = store.get_snapshot("demo", source["id"])
        metric = semantic_contract(store, tenant_id="demo", connection_id=source["id"])["metrics"][0]
        guidance = metric["query_guidance"]
        assert guidance["source_binding"] == {
            "connection_id": source["id"], "snapshot_id": current["id"], "snapshot_version": current["version"],
            "asset_id": "asset-runs", "source_pointer": "sql/postgresql/agent/research_runs",
            "field": "cost_actual_usd", "data_type": "number", "native_type": "numeric(30,12)",
            "field_id": None, "nullable": True,
        }
        assert guidance["approval"]["assertion_id"] == proposal["id"]
        assert guidance["approval"]["source_snapshot_id"] == first["id"]
        assert guidance["value_policy"]["null_measure"] == "missing"
        assert guidance["value_policy"]["numeric_zero_measure"] == "observed_value"
        assert guidance["value_policy"]["replace_missing_with_zero"] is False
        assert guidance["aggregation_policy"]["result_when_no_known_values"] == "null"
        assert guidance["diagnostics"]["required_counts"] == [
            "selected_rows", "known_measure_rows", "missing_measure_rows", "zero_measure_rows",
        ]
        assert guidance["diagnostics"]["counts_execution_state"] == "not_computed"
        assert guidance["comparison_policy"]["same_row_cohort_required"] is True
        assert guidance["comparison_policy"]["applies_to"] == "paired_measure_comparison"
        assert guidance["comparison_policy"]["paired_non_null_filter_required"] is True
        assert guidance["execution"]["executor"] == "customer_harness"
        assert guidance["execution"]["execution_verified"] is False
        assert guidance["execution"]["result_state"] == "not_computed"
        assert guidance["execution"]["preserve_declared_cohorts"] is True
        assert guidance["execution"]["sql_examples_status"] == "unexecuted"
        assert guidance["source_state"]["current_schema_verified"] is False
        assert "latest_source_schema" in guidance["execution"]["required_checks"]
        assert "sql" not in guidance
        assert metric["certified"] is False
        before_error = store.get_connection("demo", source["id"])
        store._mark_error("demo", source["id"])
        retained = semantic_contract(store, tenant_id="demo", connection_id=source["id"])["metrics"][0]["query_guidance"]
        assert retained["source_binding"]["snapshot_id"] == current["id"]
        assert retained["source_state"] == {
            "connection_status": "error", "last_error": "Curation failed; adapter details are not stored.",
            "last_refreshed_at": before_error["last_refreshed_at"], "agent_ready": False,
            "current_schema_verified": False, "freshness_state": "source_unavailable",
        }
        assert retained["execution"]["execution_verified"] is False
        # Preserving the evidence handle cannot make an absent measure executable.
        missing = {**base, "snapshot_id": "accounting-without-cost", "assets": [
            {**base["assets"][0], "schema": [{"name": "run_id", "data_type": "text"}]},
        ]}
        store.save_snapshot("demo", source["id"], missing)
        stale = semantic_contract(store, tenant_id="demo", connection_id=source["id"])
        assert stale["metrics"] == []
        assert stale["stale_assertions"][0]["id"] == proposal["id"]
    finally:
        store.close()
