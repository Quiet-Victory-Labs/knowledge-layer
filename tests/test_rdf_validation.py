"""Optional SHACL projection checks, including real discovery and review effects."""

from __future__ import annotations

import builtins
import json
import logging
import socket
from copy import deepcopy
from pathlib import Path

import pytest
from rdflib import OWL, RDF, SH, Graph, Literal, URIRef

from knowledge_layer import rdf_validation
from knowledge_layer.curation import curate_source
from knowledge_layer.ontology import KL, PROV, project_snapshot
from knowledge_layer.store import KnowledgeStore

pytest.importorskip("pyshacl", reason="Install the semantic-validation extra for SHACL checks")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def snapshot():
    return {
        "snapshot_id": "current", "status": "discovery_complete", "agent_ready": False,
        "assets": [{"id": "orders", "name": "Orders", "asset_type": "table",
                    "schema": [{"name": "amount", "data_type": "number", "nullable": None}],
                    "query_examples": [{"proposal_id": "query", "question": "Find orders",
                                        "status": "approved_unverified"}]}],
        "concepts": [{"id": "revenue", "name": "Revenue", "status": "candidate",
                      "definition": None, "evidence_ids": ["proof"]}],
        "relationships": [{"id": "link", "subject_id": "orders", "object_id": "revenue",
                           "predicate": "possible_measure", "status": "candidate",
                           "evidence_ids": ["proof"]}],
        "metrics": [{"id": "amount", "field": "amount", "source_asset_id": "orders",
                     "status": "candidate", "evidence_ids": ["proof"]}],
        "evidence": [{"id": "proof", "asset_id": "orders", "summary": "Observed schema"}],
        "proposals": [{"id": "draft", "status": "pending_review", "proposal_type": "contract_assertion",
                       "assertion_kind": "metric", "asset_id": "orders", "payload": {"measure_field": "amount"},
                       "evidence_refs": [
                           {"connection_id": "docs", "snapshot_id": "external", "evidence_id": "definition"},
                           {"connection_id": "catalog", "inventory_id": "inventory-1", "entity_id": "native:entity",
                            "evidence_id": "native:proof", "manifest_digest": "a" * 64},
                       ]}],
    }


def project(snapshot):
    return project_snapshot(snapshot, tenant_id="demo", connection_id="source")


def assert_passes(graph):
    receipt = rdf_validation.validate_graph(graph)
    assert receipt["conforms"] is True, receipt
    assert receipt["violation_count"] == 0
    return receipt


@pytest.mark.parametrize("domain", ["northstar", "juniper"])
def test_discovery_fixtures_pass_before_and_after_registry_storage(tmp_path, domain):
    snapshot = curate_source("local_files", {
        "root": str(ROOT / "data" / "connectable" / domain), "source_id": domain, "name": domain,
    })
    assert "version" not in snapshot and snapshot["agent_ready"] is False
    assert_passes(project(snapshot))
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.sqlite'}")
    try:
        source = store.create_connection("demo", kind="local_files", name=domain, config={})
        stored = store.save_snapshot("demo", source["id"], snapshot)
        assert_passes(project(stored))
    finally:
        store.close()


def test_document_only_discovery_preserves_unknown_meaning(tmp_path):
    (tmp_path / "policy.md").write_text("# Revenue\nRevenue excludes refunded orders.\n")
    snapshot = curate_source("local_files", {"root": str(tmp_path), "source_id": "docs", "name": "Docs"})
    assert snapshot["assets"] and not snapshot["metrics"]
    assert_passes(project(snapshot))


def test_optional_metadata_and_external_citations_need_no_invented_local_records(snapshot):
    graph = project(snapshot)
    citations = list(graph.subjects(RDF.type, KL.EvidenceCitation))
    inventories = list(graph.subjects(RDF.type, KL.InventoryEvidenceCitation))
    assert len(citations) == len(inventories) == 1
    assert graph.value(inventories[0], KL.sourceSnapshot) is None
    assert not list(graph.objects(graph.value(citations[0], KL.sourceSnapshot), RDF.type))
    assert_passes(graph)
    # Even an observed error/empty source is structurally valid, not agent-ready.
    assert_passes(project({"snapshot_id": "error", "status": "error", "agent_ready": False}))


@pytest.mark.parametrize("status", ["pending_review", "stale_review", "approved"])
def test_historical_proposals_need_no_current_binding_or_fabricated_evidence(snapshot, status):
    snapshot["proposals"][0].update({"status": status, "effective_on_current_snapshot": False,
                                     "source_snapshot_id": "older", "asset_id": "missing"})
    snapshot["proposals"][0]["evidence_refs"] = []
    graph = project(snapshot)
    proposal = next(graph.subjects(RDF.type, KL.Proposal))
    assert graph.value(proposal, KL.targetAsset) is None
    assert graph.value(proposal, KL.measuredField) is None
    assert_passes(graph)


def test_real_review_and_drift_pass_without_validation_publishing_meaning(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.sqlite'}")
    base = {"snapshot_id": "first", "status": "discovery_complete", "discovery_complete": True,
            "assets": [{"id": "orders", "name": "Orders", "asset_type": "table",
                        "schema": [{"name": "amount", "data_type": "number"}]}],
            "concepts": [{"id": "revenue", "name": "Revenue", "status": "candidate",
                          "evidence_ids": ["proof"]}],
            "evidence": [{"id": "proof", "asset_id": "orders", "evidence_type": "asset_metadata",
                          "summary": "Orders schema"}], "metrics": [], "relationships": [], "proposals": []}
    try:
        source = store.create_connection("demo", kind="local_files", name="Orders", config={})
        source_id = source["id"]
        store.save_snapshot("demo", source_id, base)
        definition = store.submit_proposal("demo", source_id, target_concept_id="revenue",
            proposed_definition="Revenue excludes approved refunds.", evidence_ids=["proof"],
            rationale="The owner interprets the recorded amount.")
        metric = store.submit_contract_assertion("demo", source_id, assertion_kind="metric", asset_id="orders",
            payload={"name": "Revenue", "description": "Sum of recorded amounts.", "measure_field": "amount",
                     "aggregation": "sum", "unit": "USD"}, evidence_ids=["proof"],
            rationale="The owner supplies a definition, not computed revenue.",
            expected_snapshot_id=store.get_snapshot("demo", source_id)["id"])
        assert_passes(project(store.get_snapshot("demo", source_id)))
        for proposal in [definition, metric]:
            store.decide_proposal("demo", source_id, proposal["id"], decision="approve")
        current = store.get_snapshot("demo", source_id)
        before = deepcopy(current)
        assert current["concepts"][0]["definition_status"] == "approved"
        assert current["concepts"][0]["status"] == "candidate"
        graph = project(current)
        original_triples = set(graph)
        assert_passes(graph)
        assert current == before and set(graph) == original_triples
        assert store.get_snapshot("demo", source_id) == before
        assert current["agent_ready"] is False
        # Source drift removes the metric's binding; historical review still exports.
        changed = deepcopy(base)
        changed["snapshot_id"] = "second"
        changed["assets"][0]["schema"] = [{"name": "replacement", "data_type": "text"}]
        store.save_snapshot("demo", source_id, changed)
        assert_passes(project(store.get_snapshot("demo", source_id)))
    finally:
        store.close()


@pytest.mark.parametrize("class_uri,predicate", [
    (KL.Concept, KL.status), (KL.Concept, PROV.wasDerivedFrom),
    (KL.Relationship, KL.subject), (KL.Relationship, KL.object),
    (KL.Relationship, KL.predicateName), (KL.Metric, KL.certified),
    (KL.Field, PROV.wasGeneratedBy), (KL.EvidenceCitation, KL.sourceSnapshot),
    (KL.InventoryEvidenceCitation, KL.manifestDigest), (KL.QueryExample, KL.executionVerified),
])
def test_missing_required_export_values_fail_with_actionable_paths(snapshot, class_uri, predicate):
    graph = project(snapshot)
    focus = next(graph.subjects(RDF.type, class_uri))
    graph.remove((focus, predicate, None))
    report = rdf_validation.validate_graph(graph)
    assert report["conforms"] is False
    assert any(item["focus_node"] == str(focus) and item["path"] == str(predicate)
               and item["constraint"] == "MinCountConstraintComponent" for item in report["violations"])


@pytest.mark.parametrize("class_uri,predicate,value", [
    (KL.Relationship, KL.object, Literal("private-source-text")),
    (KL.Metric, KL.certified, Literal(True)),
    (KL.Metric, KL.certified, Literal("false")),
    (KL.QueryExample, KL.executionVerified, Literal(True)),
    (KL.InventoryEvidenceCitation, KL.manifestDigest, Literal("not-a-manifest")),
    (KL.InventoryEvidenceCitation, KL.sourceSnapshot, URIRef("urn:invented:snapshot")),
    (KL.InventoryEvidenceCitation, KL.sourceTextAuthority, Literal("company_policy")),
    (KL.Snapshot, KL.agentReady, Literal("private-source-text")),
])
def test_bad_types_claims_and_inventory_pins_fail_without_echoing_values(snapshot, class_uri, predicate, value):
    graph = project(snapshot)
    focus = next(graph.subjects(RDF.type, class_uri))
    graph.set((focus, predicate, value))
    report = rdf_validation.validate_graph(graph)
    assert report["conforms"] is False
    assert any(item["path"] == str(predicate) for item in report["violations"])
    assert "private-source-text" not in json.dumps(report)


def test_approved_definition_requires_its_separate_review_evidence(snapshot):
    concept = snapshot["concepts"][0]
    concept.update(definition_status="approved", definition="Revenue excludes refunds.",
                   definition_proposal_id="review", definition_evidence_ids=["proof"])
    graph = project(snapshot)
    assert_passes(graph)
    node = next(graph.subjects(RDF.type, KL.Concept))
    graph.remove((node, KL.definitionEvidence, None))
    assert rdf_validation.validate_graph(graph)["conforms"] is False


def test_empty_export_and_automatic_identity_assertions_fail(snapshot):
    assert rdf_validation.validate_graph(Graph())["conforms"] is False
    graph = project(snapshot)
    concept = next(graph.subjects(RDF.type, KL.Concept))
    graph.add((concept, OWL.sameAs, URIRef("urn:other:revenue")))
    assert rdf_validation.validate_graph(graph)["conforms"] is False


def test_bundled_shapes_are_valid_core_and_network_imports_do_not_execute(snapshot, monkeypatch):
    from pyshacl import validate

    def no_network(*args, **kwargs):
        raise AssertionError("Offline SHACL must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    graph = project(snapshot)
    graph.add((URIRef("urn:source"), OWL.imports, URIRef("https://blocked.example/private.ttl")))
    shapes = rdf_validation.shapes_graph()
    for predicate in (SH.sparql, SH.js, SH.rule, OWL.imports):
        assert not list(shapes.triples((None, predicate, None)))
    assert validate(graph, shacl_graph=shapes, meta_shacl=True, inference="none")[0] is True
    assert_passes(graph)


def test_optional_dependency_absence_does_not_break_exports(snapshot, monkeypatch):
    real_import = builtins.__import__

    def without_validator(name, *args, **kwargs):
        if name == "pyshacl":
            raise ModuleNotFoundError("pyshacl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_validator)
    graph = project(snapshot)
    assert graph.serialize(format="turtle")
    with pytest.raises(rdf_validation.RDFValidationError, match="semantic_validation_extra_required"):
        rdf_validation.validate_graph(graph)


def test_limits_and_bounded_reports(snapshot, monkeypatch):
    graph = project(snapshot)
    monkeypatch.setattr(rdf_validation, "MAX_TRIPLES", 1)
    with pytest.raises(rdf_validation.RDFValidationError, match="graph_too_large"):
        rdf_validation.validate_graph(graph)
    monkeypatch.setattr(rdf_validation, "MAX_INPUT_BYTES", 1)
    with pytest.raises(rdf_validation.RDFValidationError, match="input_too_large"):
        rdf_validation.validate_turtle(b"xx")
    monkeypatch.setattr(rdf_validation, "MAX_TRIPLES", 50_000)
    monkeypatch.setattr(rdf_validation, "MAX_RESULTS", 1)
    for concept in graph.subjects(RDF.type, KL.Concept):
        graph.remove((concept, KL.status, None))
        graph.remove((concept, PROV.wasDerivedFrom, None))
    report = rdf_validation.validate_graph(graph)
    assert report["violation_count"] == 2
    assert len(report["violations"]) == 1 and report["violations_truncated"] is True


def test_cli_round_trip_and_failure_exit_codes(snapshot, tmp_path, capsys):
    input_file = tmp_path / "export.ttl"
    input_file.write_text(project(snapshot).serialize(format="turtle"))
    assert rdf_validation.main(["--input", str(input_file)]) == 0
    assert json.loads(capsys.readouterr().out)["conforms"] is True
    input_file.write_text("")
    assert rdf_validation.main(["--input", str(input_file)]) == 1
    assert json.loads(capsys.readouterr().out)["conforms"] is False
    input_file.write_text("private-source-marker invalid Turtle")
    assert rdf_validation.main(["--input", str(input_file)]) == 2
    output = capsys.readouterr().out
    assert "private-source-marker" not in output
    assert json.loads(output)["code"] == "invalid_turtle"
    input_file.unlink()
    assert rdf_validation.main(["--input", str(input_file)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "input_unavailable"


def test_cli_refuses_linked_or_oversized_input(tmp_path, capsys, monkeypatch):
    input_file = tmp_path / "export.ttl"
    input_file.write_bytes(b"too big")
    linked = tmp_path / "linked.ttl"
    linked.symlink_to(input_file)
    assert rdf_validation.main(["--input", str(linked)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "input_unavailable"
    monkeypatch.setattr(rdf_validation, "MAX_INPUT_BYTES", 1)
    assert rdf_validation.main(["--input", str(input_file)]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "input_too_large"


def test_typed_literal_diagnostics_are_private_and_logging_is_restored(snapshot, tmp_path, capsys, caplog):
    input_file = tmp_path / "export.ttl"
    turtle = project(snapshot).serialize(format="turtle")
    turtle += ('\n<urn:private:subject> <urn:private:property> "private-source-marker"'
               '^^<http://www.w3.org/2001/XMLSchema#integer> .\n')
    input_file.write_text(turtle)
    validator_logger = logging.getLogger("pyshacl-validate")
    before = (tuple(validator_logger.handlers), validator_logger.level, tuple(validator_logger.filters))
    with caplog.at_level(logging.WARNING, logger="rdflib.term"):
        assert rdf_validation.main(["--input", str(input_file)]) == 0
        output = capsys.readouterr()
        assert "private-source-marker" not in output.out + output.err + caplog.text
        logging.getLogger("rdflib.term").warning("normal diagnostics restored")
    assert "normal diagnostics restored" in caplog.text
    assert (tuple(validator_logger.handlers), validator_logger.level, tuple(validator_logger.filters)) == before
