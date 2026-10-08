"""The ontology export must retain review state, provenance, and versions."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import OWL, RDF, RDFS, Graph, Literal

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.contracts import validate_assertion
from knowledge_layer.ontology import KL, PROV, project_snapshot


def _snapshot(version: int) -> dict:
    return {
        "id": f"snapshot-{version}",
        "version": version,
        "created_at": "2026-09-30T00:00:00Z",
        "status": "discovery_complete",
        "agent_ready": False,
        "assets": [{"id": "asset-1", "name": "Invoices", "status": "observed"}],
        "concepts": [
            {
                "id": "concept-1",
                "name": "Invoice",
                "status": "candidate",
                "trust_state": "inferred",
                "evidence_ids": ["evidence-1"],
            },
            {
                "id": "concept-2",
                "name": "Bill",
                "status": "candidate",
                "trust_state": "inferred",
                "evidence_ids": ["evidence-1"],
            },
        ],
        "evidence": [
            {"id": "evidence-1", "asset_id": "asset-1", "summary": "Name observed in schema"}
        ],
        "relationships": [
            {
                "id": "relation-1",
                "subject_id": "concept-1",
                "object_id": "concept-2",
                "predicate": "possible_synonym",
                "status": "candidate",
                "evidence_ids": ["evidence-1"],
            }
        ],
        "metrics": [],
        "proposals": [],
    }


def test_pending_meaning_text_is_reviewable_without_becoming_a_definition():
    from rdflib.namespace import SKOS

    snapshot = _snapshot(1)
    snapshot["proposals"] = [{
        "id": "pending-meaning", "proposal_type": "semantic_correction",
        "target_concept_id": "concept-1", "status": "pending_review",
        "proposed_definition": "Proposed scoped company definition.",
        "evidence_ids": ["evidence-1"],
    }]
    graph = project_snapshot(snapshot, tenant_id="demo", connection_id="source-a")
    assert Literal("Proposed scoped company definition.") in graph.objects(None, KL.proposedDefinition)
    assert Literal("Proposed scoped company definition.") not in graph.objects(None, SKOS.definition)


def test_ontology_projection_preserves_candidate_relationship_and_evidence():
    graph = project_snapshot(_snapshot(1), tenant_id="demo", connection_id="source-1")
    relationship = next(graph.subjects(RDF.type, KL.Relationship))
    concept = next(graph.subjects(KL.trustState, Literal("inferred")))
    assert (relationship, KL.status, Literal("candidate")) in graph
    assert any(graph.objects(relationship, PROV.wasDerivedFrom))
    assert any(graph.objects(concept, PROV.wasDerivedFrom))
    assert list(graph.triples((None, OWL.sameAs, None))) == []
    assert len(Graph().parse(data=graph.serialize(format="turtle"), format="turtle")) == len(graph)


def test_versioned_ontology_http_export_requires_agent_token():
    class Store:
        def get_snapshot(self, tenant_id, connection_id, version=None):
            assert tenant_id == "demo"
            if connection_id != "source-1" or version not in {None, 1}:
                return None
            return _snapshot(version or 2)

    app = FastAPI()
    app.mount("/agent", AgentBearerBoundary(build_agent_api(Store()), "known-token"))
    with TestClient(app) as client:
        assert client.get("/agent/sources/source-1/ontology.ttl").status_code == 401
        current = client.get(
            "/agent/sources/source-1/ontology.ttl",
            headers={"Authorization": "Bearer known-token"},
        )
        older = client.get(
            "/agent/sources/source-1/ontology.ttl?version=1",
            headers={"Authorization": "Bearer known-token"},
        )
        missing = client.get(
            "/agent/sources/source-1/ontology.ttl?version=3",
            headers={"Authorization": "Bearer known-token"},
        )
    assert current.status_code == 200
    assert current.headers["content-type"].startswith("text/turtle")
    assert "snapshot-2" in current.text
    assert "snapshot-1" in older.text
    assert missing.status_code == 404


def test_metric_rdf_can_recover_exact_measure_binding_and_review_limits():
    snapshot = _snapshot(1)
    snapshot["assets"][0].update({
        "source_pointer": "control/agent.sim_experiment_run",
        "schema": [{"name": "cost_actual_usd", "data_type": "number",
                    "native_type": "double precision", "nullable": True}],
    })
    snapshot["metrics"] = [{
        "id": "metric-1", "name": "Recorded run cost", "source_asset_id": "asset-1",
        "field": "cost_actual_usd", "aggregation": "sum", "unit": "USD",
        "description": "Sum of supplied run costs; missing is unknown, not billed spend.",
        "status": "approved", "trust_state": "owner_reviewed", "certified": False,
        "evidence_ids": ["evidence-1"],
    }]
    graph = Graph().parse(data=project_snapshot(
        snapshot, tenant_id="demo", connection_id="source-1").serialize(format="turtle"),
        format="turtle")
    metric = next(graph.subjects(RDF.type, KL.Metric))
    asset = next(graph.objects(metric, KL.sourceAsset))
    field = next(graph.objects(metric, KL.measuredField))
    assert (asset, KL.sourcePointer, Literal("control/agent.sim_experiment_run")) in graph
    assert (asset, KL.hasField, field) in graph
    assert (field, RDFS.label, Literal("cost_actual_usd")) in graph
    assert (field, KL.nativeType, Literal("double precision")) in graph
    assert (field, KL.nullable, Literal(True)) in graph
    assert (metric, KL.measureField, Literal("cost_actual_usd")) in graph
    assert (metric, KL.aggregation, Literal("sum")) in graph
    assert (metric, KL.unit, Literal("USD")) in graph
    assert (metric, RDFS.comment, Literal(snapshot["metrics"][0]["description"])) in graph
    assert (metric, KL.status, Literal("approved")) in graph
    assert (metric, KL.certified, Literal(False)) in graph
    assert any(graph.objects(metric, PROV.wasDerivedFrom))


def test_rdf_never_creates_a_measure_binding_for_missing_source_fields():
    snapshot = _snapshot(1)
    snapshot["assets"][0]["schema"] = [
        {"name": "run_id", "data_type": "text", "field_id": 1, "nullable": False},
        {"name": "unknown_nullability", "data_type": "text", "nullable": None},
    ]
    snapshot["metrics"] = [
        {"id": "missing-field", "source_asset_id": "asset-1", "field": "absent_cost"},
        {"id": "missing-asset", "source_asset_id": "absent-asset", "field": "run_id"},
    ]
    graph = project_snapshot(snapshot, tenant_id="demo", connection_id="source-1")
    assert not list(graph.triples((None, KL.measuredField, None)))
    assert len(list(graph.subjects(RDF.type, KL.Field))) == 2
    field = next(graph.subjects(RDFS.label, Literal("run_id")))
    assert (field, KL.fieldId, Literal(1)) in graph
    assert (field, KL.nullable, Literal(False)) in graph
    unknown = next(graph.subjects(RDFS.label, Literal("unknown_nullability")))
    assert not list(graph.objects(unknown, KL.nullable))


def test_pending_metric_rdf_preserves_payload_and_snapshot_pinned_document_citation():
    snapshot = _snapshot(1)
    snapshot["assets"][0]["schema"] = [{"name": "cost_actual_usd", "data_type": "number"}]
    payload = {
        "name": "Recorded run cost", "description": "Known run costs, not billed cloud charges.",
        "measure_field": "cost_actual_usd", "aggregation": "sum", "unit": "USD",
    }
    snapshot["proposals"] = [{
        "id": "proposal-1", "proposal_type": "contract_assertion", "assertion_kind": "metric",
        "asset_id": "asset-1", "status": "pending_review", "payload": payload,
        "evidence_ids": ["evidence-1"],
        "evidence_refs": [{"connection_id": "docs-1", "snapshot_id": "docs-snapshot-2",
                           "evidence_id": "cost-definition"}],
    }]
    graph = Graph().parse(data=project_snapshot(
        snapshot, tenant_id="demo", connection_id="source-1").serialize(format="turtle"),
        format="turtle")
    proposal = next(graph.subjects(RDF.type, KL.Proposal))
    assert not list(graph.subjects(RDF.type, KL.Metric))
    assert (proposal, KL.status, Literal("pending_review")) in graph
    assert (proposal, KL.assertionKind, Literal("metric")) in graph
    encoded = next(graph.objects(proposal, KL.assertionPayload))
    assert encoded.datatype == RDF.JSON
    assert json.loads(str(encoded)) == payload
    assert (proposal, KL.unit, Literal("USD")) in graph
    field = next(graph.objects(proposal, KL.measuredField))
    assert (field, RDF.type, KL.Field) in graph
    citation = next(graph.objects(proposal, KL.hasEvidenceCitation))
    assert (citation, RDF.type, KL.EvidenceCitation) in graph
    assert str(next(graph.objects(citation, KL.sourceSnapshot))) == (
        "urn:knowledge:demo:docs-1:snapshot:docs-snapshot-2")
    external_evidence = next(graph.objects(citation, KL.evidence))
    assert str(external_evidence) == "urn:knowledge:demo:docs-1:evidence:cost-definition"
    assert (proposal, PROV.wasDerivedFrom, external_evidence) in graph
    assert not list(graph.triples((None, OWL.sameAs, None)))


def test_rdf_contract_payload_round_trip_is_exact_beyond_presentation_string_limit():
    snapshot = _snapshot(1)
    snapshot["assets"][0].update({"name": "runs", "asset_type": "table",
                                 "schema": [{"name": "run_id", "data_type": "text"}]})
    payload = {
        "question": "Which registered runs belong in the analyst's review? " * 6,
        "dialect": "ANSI_SQL",
        "sql": "SELECT run_id FROM runs WHERE run_id IN (" + ",".join(
            f"'{i:032d}'" for i in range(110)) + ")",
        "expected_grain": "One observed registered run per identifier.",
    }
    payload = validate_assertion(snapshot, kind="query_example", asset_id="asset-1",
                                 payload=payload, evidence_ids=["evidence-1"], source_kind="postgres")
    assert len(json.dumps(payload)) > 4000
    assert len(payload["sql"]) <= 4000
    snapshot["proposals"] = [{
        "id": "proposal-1", "proposal_type": "contract_assertion", "assertion_kind": "query_example",
        "asset_id": "asset-1", "status": "stale_review", "payload": payload,
        "evidence_refs": [{"connection_id": "docs-1"}],
    }]
    graph = Graph().parse(data=project_snapshot(
        snapshot, tenant_id="demo", connection_id="source-1").serialize(format="turtle"),
        format="turtle")
    proposal = next(graph.subjects(RDF.type, KL.Proposal))
    assert (proposal, KL.status, Literal("stale_review")) in graph
    assert json.loads(str(next(graph.objects(proposal, KL.assertionPayload)))) == payload
    assert not list(graph.subjects(RDF.type, KL.EvidenceCitation))


def test_rdf_preserves_historical_approval_without_current_measure_binding():
    snapshot = _snapshot(2)
    snapshot["assets"][0]["schema"] = [{"name": "cost_actual_usd", "data_type": "number"}]
    snapshot["proposals"] = [{
        "id": "proposal-1", "proposal_type": "contract_assertion", "assertion_kind": "metric",
        "asset_id": "asset-1", "status": "approved", "effective_on_current_snapshot": False,
        "source_snapshot_id": "snapshot-1", "payload": {
            "name": "Recorded cost", "description": "Sum of supplied run costs.",
            "measure_field": "cost_actual_usd", "aggregation": "sum", "unit": "USD",
        },
    }]
    graph = project_snapshot(snapshot, tenant_id="demo", connection_id="source-1")
    proposal = next(graph.subjects(RDF.type, KL.Proposal))
    assert (proposal, KL.status, Literal("approved")) in graph
    assert (proposal, KL.effectiveOnCurrentSnapshot, Literal(False)) in graph
    assert str(next(graph.objects(proposal, KL.sourceSnapshot))) == (
        "urn:knowledge:demo:source-1:snapshot:snapshot-1")
    assert not list(graph.objects(proposal, KL.measuredField))
    assert not list(graph.objects(proposal, KL.targetAsset))
    assert (proposal, KL.targetAssetId, Literal("asset-1")) in graph


def test_rdf_preserves_entire_observed_nested_physical_type():
    snapshot = _snapshot(1)
    native_type = "struct<" + ",".join(f"field_{i}:string" for i in range(500)) + ">"
    assert len(native_type) > 4000
    snapshot["assets"][0]["schema"] = [
        {"name": "payload", "data_type": "structured", "native_type": native_type},
    ]
    graph = Graph().parse(data=project_snapshot(
        snapshot, tenant_id="demo", connection_id="source-1").serialize(format="turtle"),
        format="turtle")
    field = next(graph.subjects(RDF.type, KL.Field))
    assert str(next(graph.objects(field, KL.nativeType))) == native_type


def test_rdf_preserves_definition_review_separately_from_candidate_concept():
    snapshot = _snapshot(1)
    snapshot['concepts'][0].update({
        'definition': 'A reviewed definition whose identity mapping remains a candidate.',
        'definition_status': 'approved', 'definition_proposal_id': 'proposal-1',
        'definition_evidence_ids': ['evidence-1'],
        'definition_decided_at': '2026-10-02T00:00:00Z',
        'definition_decision_rationale': 'Isolated evaluator review, not production certification.',
    })
    graph = project_snapshot(snapshot, tenant_id='demo', connection_id='source-1')
    concept = next(graph.subjects(KL.definitionStatus, Literal('approved')))
    assert (concept, KL.status, Literal('candidate')) in graph
    assert (concept, KL.trustState, Literal('inferred')) in graph
    assert str(next(graph.objects(concept, KL.definitionProposal))).endswith(':proposals:proposal-1')
    assert str(next(graph.objects(concept, KL.definitionEvidence))).endswith(':evidence:evidence-1')
    assert (concept, KL.definitionDecisionRationale,
            Literal('Isolated evaluator review, not production certification.')) in graph
    assert list(graph.triples((None, OWL.sameAs, None))) == []


def _mcp_export_app(store):
    from contextlib import asynccontextmanager

    from knowledge_layer.mcp import build_knowledge_mcp_server

    server = build_knowledge_mcp_server(store)

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount('/mcp', AgentBearerBoundary(server.streamable_http_app(
        streamable_http_path='/', json_response=True, stateless_http=True,
    ), 'known-token'))
    return app


def _export_call(client, arguments):
    return client.post('/mcp/', headers={
        'Authorization': 'Bearer known-token', 'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2025-06-18',
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
             'params': {'name': 'get_ontology_export', 'arguments': arguments}})


def test_mcp_rdf_export_is_discoverable_versioned_complete_and_authenticated():
    class Store:
        def get_snapshot(self, tenant_id, connection_id, version=None):
            assert tenant_id == 'demo'
            return _snapshot(version or 2) if connection_id == 'source-1' and version in {None, 1} else None

    with TestClient(_mcp_export_app(Store()), base_url="http://127.0.0.1:8765") as client:
        assert client.post('/mcp/', json={}).status_code == 401
        listing = client.post('/mcp/', headers={
            'Authorization': 'Bearer known-token', 'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-06-18',
        }, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}).json()
        assert 'get_ontology_export' in {t['name'] for t in listing['result']['tools']}
        for version in [None, 1]:
            arguments = {'connection_id': 'source-1', **({'version': version} if version else {})}
            result = _export_call(client, arguments).json()['result']
            assert result['isError'] is False
            payload = result['structuredContent']
            assert json.loads(result['content'][0]['text']) == payload
            assert payload['snapshot_id'] == f'snapshot-{version or 2}'
            graph = Graph().parse(data=payload['turtle'], format='turtle')
            expected = project_snapshot(_snapshot(version or 2), tenant_id='demo', connection_id='source-1')
            assert set(graph) == set(expected)
        for arguments in [{'connection_id': 'missing'}, {'connection_id': 'source-1', 'version': 3},
                          {'connection_id': 'source-1', 'version': 0}]:
            result = _export_call(client, arguments).json()['result']
            assert result['isError'] is True
            assert not result.get('structuredContent')


def test_mcp_rdf_export_cannot_bypass_snapshot_access_or_clip_oversized_content(monkeypatch):
    from knowledge_layer import mcp
    from knowledge_layer.store import KnowledgeAccessDenied

    class Store:
        def get_snapshot(self, tenant_id, connection_id, version=None):
            if connection_id == 'denied':
                raise KnowledgeAccessDenied('Snapshot access denied')
            snapshot = _snapshot(1)
            snapshot['concepts'][0]['definition'] = 'private-source-marker-' + 'Ω' * 200
            return snapshot

    monkeypatch.setattr(mcp, 'MAX_CONTRACT_RESPONSE_BYTES', 100)
    with TestClient(_mcp_export_app(Store()), base_url="http://127.0.0.1:8765") as client:
        denied = _export_call(client, {'connection_id': 'denied'}).json()['result']
        refused = _export_call(client, {'connection_id': 'source-1'}).json()['result']
    assert denied['isError'] is True
    assert refused['isError'] is True
    assert not refused.get('structuredContent')
    assert 'ontology_response_too_large' in refused['content'][0]['text']
    assert 'private-source-marker' not in json.dumps(refused)
