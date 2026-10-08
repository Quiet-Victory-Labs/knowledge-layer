"""Collective source traversal preserves scoped meaning, evidence and grants."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import OWL, RDF, Graph

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.store import KnowledgeNotFound, KnowledgeStore, KnowledgeValidationError
from knowledge_layer.workspace_graph import _Graph, workspace_graph, workspace_ontology


@pytest.fixture
def company(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    sources = {}
    for name, asset_type, meaning in (
        ("Warehouse", "table", "Recorded realized contest points."),
        ("Source documentation", "document", "Expected points at projection time."),
        ("Native glossary", "semantic_definition", "Realized player contest scoring."),
    ):
        cid = store.create_connection("demo", name=name, kind="local_files", config={})["id"]
        sources[name] = cid
        store.save_snapshot("demo", cid, {
            "status": "discovery_complete", "discovery_complete": True,
            "agent_ready": False, "meaning_certified": False,
            "assets": [{"id": "same-asset", "name": name, "asset_type": asset_type,
                        "schema": [{"name": "points", "native_type": "UInt64", "nullable": False}]
                        if asset_type == "table" else []}],
            "concepts": [{"id": "same-concept", "name": "Points", "definition": meaning,
                          "status": "candidate", "semantic_type": "source_entity",
                          "provenance": [{"asset_id": "same-asset"}], "evidence_ids": ["same-evidence"]}],
            "evidence": [{"id": "same-evidence", "asset_id": "same-asset", "summary": meaning,
                          "details": {"field": "points"} if asset_type == "table" else {},
                          "evidence_type": "document_derived_term" if asset_type == "document" else "asset_metadata"}],
            "relationships": [], "metrics": [], "proposals": [],
        })
    yield store, sources
    store.close()


def test_collective_graph_connects_scoped_terms_fields_documents_and_imports(company):
    store, sources = company
    value = workspace_graph(store, tenant_id="demo", q="points", limit=180, depth=3)
    nodes = value["nodes"]
    ids = {node["id"] for node in nodes}
    assert len(ids) == len(nodes)
    assert {node["kind"] for node in nodes} >= {"field", "document", "ontology", "concept", "shared_term", "source"}
    assert {node["connection_id"] for node in nodes if node["kind"] == "concept"} == set(sources.values())
    hub = next(node for node in nodes if node["kind"] == "shared_term")
    assert hub["source_count"] == 3 and hub["definition_variant_count"] == 3
    assert hub["status"] == "candidate" and hub["conflict"] is True
    assert value["coverage"]["matching_definitions_are_equivalent"] is False
    assert len({node["description"] for node in nodes if node["kind"] == "concept"}) == 3
    assert all(edge["subject_id"] in ids and edge["object_id"] in ids for edge in value["edges"])
    assert all(edge["status"] == "candidate" for edge in value["edges"] if edge["predicate"] == "possible_meaning_in")
    field = next(node for node in nodes if node["kind"] == "field")
    assert field["native_type"] == "UInt64" and field["nullable"] is False
    # A schema field remains navigable even before its description is approved.
    focused = workspace_graph(store, tenant_id="demo", focus_id=field["id"], depth=2)
    assert hub["id"] in {node["id"] for node in focused["nodes"]}
    assert "document" in {node["kind"] for node in workspace_graph(store, tenant_id="demo", focus_id=hub["id"], depth=2)["nodes"]}


def test_scope_and_grant_revocation_remove_hubs_and_private_bindings(company):
    store, sources = company
    reader = Principal("reader", frozenset())
    cid = sources["Warehouse"]
    store.grant_source_access("demo", cid, principal_type="user", principal_id="reader", role="read")
    full = workspace_graph(store, tenant_id="demo")
    private_node = next(node["id"] for node in full["nodes"] if node.get("connection_id") == sources["Native glossary"])
    store.access_enforced = True
    with principal_scope(reader):
        value = workspace_graph(store, tenant_id="demo")
        encoded = json.dumps(value)
        assert value["total_sources"] == 1
        assert sources["Native glossary"] not in encoded
        assert "Realized player contest scoring" not in encoded
        assert not any(node["kind"] == "shared_term" for node in value["nodes"])
        with pytest.raises(KnowledgeNotFound):
            workspace_graph(store, tenant_id="demo", focus_id=private_node)
        with pytest.raises(KnowledgeNotFound):
            workspace_graph(store, tenant_id="demo", connection_id=sources["Native glossary"])


def test_current_citation_traversal_and_drift_do_not_create_equivalence(company):
    store, sources = company
    cid, docs = sources["Warehouse"], sources["Source documentation"]
    snapshot = store.get_snapshot("demo", cid)
    doc_snapshot = store.get_snapshot("demo", docs)
    # Use the current proposal ledger rather than editing source metadata.
    draft = store.submit_contract_assertion(
        "demo", cid, assertion_kind="field_description", asset_id="same-asset",
        payload={"field": "points", "description": "Recorded realized points with source scope."},
        evidence_ids=["same-evidence"], evidence_refs=[{
            "connection_id": docs, "snapshot_id": doc_snapshot["id"], "evidence_id": "same-evidence",
        }], expected_snapshot_id=snapshot["id"], rationale="Source declarations describe the current field scope.",
    )
    value = workspace_graph(store, tenant_id="demo", connection_id=cid, q="points", depth=3)
    assert {source["id"] for source in value["connections"]} == {cid, docs}
    assertion = next(node for node in value["nodes"] if node["record_id"] == draft["id"])
    assert assertion["description"] == draft["payload"]["description"]
    assert assertion["status"] == "pending_review"
    assert any(edge["predicate"] == "cites_term" and edge["cross_source"] for edge in value["edges"])
    store.save_snapshot("demo", docs, {"assets": [], "concepts": [], "evidence": [], "relationships": []})
    changed = workspace_graph(store, tenant_id="demo", connection_id=cid)
    assert not any(edge["predicate"] == "cites_term" for edge in changed["edges"])
    assert not any(edge["predicate"] in {"same_as", "exact_match"} for edge in changed["edges"])


def test_source_pages_search_focus_and_rdf_share_the_same_model(company):
    store, _sources = company
    first = workspace_graph(store, tenant_id="demo", source_limit=1, limit=10)
    assert first["loaded_sources"] == 1 and first["next_cursor"] and first["truncated"]
    second = workspace_graph(store, tenant_id="demo", source_limit=1, cursor=first["next_cursor"])
    assert first["connections"][0]["id"] != second["connections"][0]["id"]
    earlier_node = first["nodes"][0]["id"]
    later_focus = workspace_graph(store, tenant_id="demo", source_limit=1,
                                  cursor=first["next_cursor"], focus_id=earlier_node)
    assert later_focus["nodes"] == [] and later_focus["next_cursor"]
    with pytest.raises(KnowledgeValidationError):
        workspace_graph(store, tenant_id="other", cursor=first["next_cursor"])
    with pytest.raises(KnowledgeValidationError):
        workspace_graph(store, tenant_id="demo", limit=401)
    assert workspace_graph(store, tenant_id="demo", q="nonexistent term")["nodes"] == []
    value = workspace_graph(store, tenant_id="demo", q="points")
    exported = workspace_ontology(value)
    rdf = Graph().parse(data=exported["content"], format="turtle")
    assert len(rdf) == exported["triple_count"]
    assert not list(rdf.triples((None, OWL.sameAs, None)))
    assert len(list(rdf.triples((None, RDF.type, None)))) == len(value["nodes"]) + len(value["edges"]) + 1
    from knowledge_layer.rdf_validation import validate_graph

    assert validate_graph(rdf, schema="workspace")["conforms"] is True


def test_http_and_both_mcp_surfaces_expose_collective_graph(company):
    store, _sources = company
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store))
    with TestClient(app) as client:
        graph = client.get("/api/knowledge/workspace/graph", params={"q": "points"})
        assert graph.status_code == 200
        assert graph.json()["loaded_sources"] == 3
        assert client.get("/api/knowledge/workspace/ontology").json()["media_type"] == "text/turtle"
        assert client.get("/api/knowledge/workspace/graph?limit=401").status_code == 422

    async def inspect():
        for server in (build_quick_mcp_server(store), build_knowledge_mcp_server(store)):
            tools = {tool.name: tool for tool in await server.list_tools()}
            assert tools["get_workspace_graph"].annotations.read_only_hint is True
            result = await server.call_tool("get_workspace_graph", {"q": "points"})
            assert not result.is_error and result.structured_content["loaded_sources"] == 3
            rdf = await server.call_tool("get_workspace_ontology", {"q": "points"})
            assert rdf.structured_content["triple_count"] > 0
    asyncio.run(inspect())


def test_reviewed_binding_keeps_independent_pending_claim_and_rdf_provenance():
    source = {"id": "warehouse", "name": "Warehouse", "kind": "local_files", "snapshot": {
        "id": "warehouse-current", "assets": [{"id": "table", "name": "Orders", "schema": [{"name": "points"}]}],
        "evidence": [{"id": "observed-field", "asset_id": "table"}],
        "relationships": [{"subject_id": "table", "source_asset_id": "table", "source_field": "points",
                           "object_id": "term", "to_connection_id": "docs", "predicate": "measures",
                           "status": "approved", "trust_state": "owner_reviewed", "decision_proposal_id": "reviewed",
                           "evidence_ids": ["observed-field"]}],
        "proposals": [{"id": pid, "asset_id": "table", "assertion_kind": "term_binding", "status": status,
                       "payload": {"field": "points", "relationship": "measures", "target_connection_id": "docs",
                                   "target_snapshot_id": "docs-current", "target_concept_id": "term"},
                       "evidence_ids": ["observed-field"], "evidence_refs": [{"connection_id": "docs",
                         "snapshot_id": "docs-current", "evidence_id": "definition"}]}
                      for pid, status in (("reviewed", "approved"), ("new-claim", "pending_review"))],
    }}
    docs = {"id": "docs", "name": "Policy", "kind": "local_files", "snapshot": {
        "id": "docs-current", "concepts": [{"id": "term", "name": "Points", "status": "candidate",
            "definition": "Reviewed points scope.", "definition_status": "approved",
            "definition_proposal_id": "meaning-review", "evidence_ids": ["definition"]}],
        "evidence": [{"id": "definition"}],
    }}
    builder = _Graph("demo", [source, docs])
    builder.build()
    nodes, edges, clipped = builder.select(q="points", focus_id=None, depth=3, limit=100)
    bindings = [edge for edge in edges if edge["predicate"] == "measures"]
    assert len(bindings) == 2
    assert {edge["status"] for edge in bindings} == {"approved", "pending_review"}
    assert all(len(edge["evidence_ids"]) == 2 and edge["assertion_id"] for edge in bindings)
    concept = next(node for node in nodes if node["kind"] == "concept")
    assert concept["status"] == "candidate" and concept["definition_status"] == "approved"
    assert concept["definition_trust_state"] == "owner_reviewed"
    # Depth-zero export may omit claim/evidence nodes; exact provenance IRIs survive.
    from knowledge_layer.ontology import KL
    rdf = Graph().parse(data=workspace_ontology({"nodes": nodes, "edges": bindings,
        "coverage": {}, "next_cursor": None, "truncated": clipped, "trust_policy": "Scoped claims."})["content"], format="turtle")
    assert len(list(rdf.triples((None, KL.decisionProposal, None)))) == 2
    assert len(list(rdf.triples((None, KL.evidence, None)))) >= 4
    assert len(list(rdf.triples((None, KL.definitionStatus, None)))) == 1


def test_composite_joins_remain_asset_links_and_clipped_edges_report_truncation():
    source = {"id": "warehouse", "name": "Warehouse", "snapshot": {
        "assets": [{"id": asset_id, "name": asset_id, "schema": [{"name": "key_a"}, {"name": "key_b"}]}
                   for asset_id in ("orders", "customers")],
        "relationships": [{"subject_id": "orders", "object_id": "customers", "predicate": "joins_to",
            "source_asset_id": "orders", "source_field": "key_a, key_b", "status": "approved"}],
    }}
    builder = _Graph("demo", [source])
    builder.build()
    join, = [edge for edge in builder.edges.values() if edge["predicate"] == "joins_to"]
    assert builder.nodes[join["subject_id"]]["record_id"] == "orders"
    dense = _Graph("demo", [])
    ids = [dense.node("source", "concept", str(index), "Points") for index in range(20)]
    for left in ids:
        for right in ids:
            dense.edge(left, "related_to", right)
    nodes, edges, clipped = dense.select(q="points", focus_id=None, depth=0, limit=30)
    assert len(nodes) == 20 and len(edges) == 150 and clipped is True
