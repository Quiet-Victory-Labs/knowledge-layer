"""Portable RDF projection of a versioned knowledge snapshot.

This graph contains metadata, proposed meaning, evidence handles, and decisions.
It deliberately does not copy source rows or turn candidate links into owl:sameAs.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

KL = Namespace("https://knowledge-layer.example/ontology#")
PROV = Namespace("http://www.w3.org/ns/prov#")
SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")


def _uri(tenant_id: str, connection_id: str, kind: str, item_id: str) -> URIRef:
    parts = (tenant_id, connection_id, kind, item_id)
    return URIRef("urn:knowledge:" + ":".join(quote(part, safe="") for part in parts))


def _identifier(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("snapshot_id") or "")


def _literal(value: Any) -> Literal | None:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value, datatype=XSD.integer)
    if isinstance(value, float):
        return Literal(value, datatype=XSD.double)
    return Literal(str(value))


def _add_literal(graph: Graph, node: URIRef, predicate: URIRef, value: Any) -> None:
    literal = _literal(value)
    if literal is not None:
        graph.add((node, predicate, literal))


def _measure_binding(
    graph: Graph, node: URIRef, *, tenant_id: str, connection_id: str,
    assets: dict[str, dict[str, Any]], asset_id: Any, field_name: Any,
) -> None:
    """Retain a declared measure; link only to an actually observed source field."""
    _add_literal(graph, node, KL.measureField, field_name)
    asset = assets.get(str(asset_id))
    if asset is None:
        return
    graph.add((node, KL.sourceAsset, _uri(tenant_id, connection_id, "assets", asset["id"])))
    if isinstance(field_name, str) and field_name and any(
           isinstance(field, dict) and field.get("name") == field_name
           for field in asset.get("schema") or []):
        graph.add((node, KL.measuredField, _uri(
            tenant_id, connection_id, "field", f"{asset['id']}:{field_name}")))


def project_snapshot(
    snapshot: dict[str, Any], *, tenant_id: str, connection_id: str
) -> Graph:
    """Project one source snapshot as an evidence-preserving RDF graph."""
    graph = Graph()
    graph.bind("kl", KL)
    graph.bind("prov", PROV)
    graph.bind("skos", SKOS)
    for class_uri, label in (
        (KL.Connection, "Source connection"),
        (KL.Snapshot, "Versioned knowledge snapshot"),
        (KL.Asset, "Discovered source asset"),
        (KL.Field, "Observed source field with reviewable meaning"),
        (KL.Concept, "Business concept candidate or approved definition"),
        (KL.Evidence, "Source evidence handle"),
        (KL.Relationship, "Typed relationship assertion"),
        (KL.Metric, "Metric candidate or certified metric"),
        (KL.Proposal, "Reviewable semantic proposal"),
        (KL.EvidenceCitation, "Snapshot-pinned external evidence citation"),
        (KL.QueryExample, "Owner-reviewed, unexecuted query example"),
    ):
        graph.add((class_uri, RDF.type, RDFS.Class))
        graph.add((class_uri, RDFS.label, Literal(label)))

    snapshot_id = str(snapshot.get("id") or snapshot.get("snapshot_id") or "unversioned")
    snapshot_node = _uri(tenant_id, connection_id, "snapshot", snapshot_id)
    connection_node = _uri(tenant_id, connection_id, "connection", connection_id)
    graph.add((snapshot_node, RDF.type, KL.Snapshot))
    graph.add((connection_node, RDF.type, KL.Connection))
    graph.add((snapshot_node, KL.sourceConnection, connection_node))
    _add_literal(graph, snapshot_node, KL.version, snapshot.get("version"))
    _add_literal(graph, snapshot_node, KL.createdAt, snapshot.get("created_at"))
    _add_literal(graph, snapshot_node, KL.status, snapshot.get("status"))
    _add_literal(graph, snapshot_node, KL.agentReady, snapshot.get("agent_ready", False))
    assets_by_id = {str(item["id"]): item for item in snapshot.get("assets", [])
                    if isinstance(item, dict) and item.get("id")}
    asset_ids = set(assets_by_id)

    for collection, class_uri in (
        ("assets", KL.Asset),
        ("concepts", KL.Concept),
        ("evidence", KL.Evidence),
        ("relationships", KL.Relationship),
        ("metrics", KL.Metric),
        ("proposals", KL.Proposal),
    ):
        for item in snapshot.get(collection, []):
            if not isinstance(item, dict):
                continue
            item_id = _identifier(item)
            if not item_id:
                continue
            node = _uri(tenant_id, connection_id, collection, item_id)
            graph.add((node, RDF.type, class_uri))
            graph.add((node, PROV.wasGeneratedBy, snapshot_node))
            _add_literal(graph, node, RDFS.label, item.get("name") or item.get("summary"))
            _add_literal(graph, node, KL.status, item.get("status"))
            _add_literal(graph, node, KL.trustState, item.get("trust_state"))
            _add_literal(graph, node, KL.sourcePointer, item.get("source_pointer"))
            _add_literal(graph, node, KL.confidence, item.get("confidence"))
            _add_literal(graph, node, KL.effectiveOnCurrentSnapshot,
                         item.get("effective_on_current_snapshot"))
            if isinstance(item.get("source_snapshot_id"), str) and item["source_snapshot_id"]:
                graph.add((node, KL.sourceSnapshot, _uri(
                    tenant_id, connection_id, "snapshot", item["source_snapshot_id"])))
            if collection == "assets":
                _add_literal(graph, node, RDFS.comment, item.get("description"))
                grain = item.get("grain_contract") or {}
                _add_literal(graph, node, KL.grainDescription, grain.get("description"))
                for key_field in grain.get("key_fields") or []:
                    _add_literal(graph, node, KL.grainKeyField, key_field)
                for field in item.get("schema") or []:
                    if not isinstance(field, dict) or not field.get("name"):
                        continue
                    field_node = _uri(tenant_id, connection_id, "field", f"{item_id}:{field['name']}")
                    graph.add((field_node, RDF.type, KL.Field))
                    graph.add((field_node, PROV.wasGeneratedBy, snapshot_node))
                    graph.add((field_node, PROV.wasDerivedFrom, node))
                    graph.add((node, KL.hasField, field_node))
                    _add_literal(graph, field_node, RDFS.label, field["name"])
                    _add_literal(graph, field_node, RDFS.comment, field.get("description"))
                    _add_literal(graph, field_node, KL.datatype, field.get("data_type"))
                    _add_literal(graph, field_node, KL.nativeType, field.get("native_type"))
                    _add_literal(graph, field_node, KL.fieldId, field.get("field_id"))
                    _add_literal(graph, field_node, KL.nullable, field.get("nullable"))
                    _add_literal(graph, field_node, KL.status, field.get("description_status") or "observed_schema")
                    for evidence_id in field.get("description_evidence_ids") or []:
                        graph.add((field_node, PROV.wasDerivedFrom,
                                   _uri(tenant_id, connection_id, "evidence", str(evidence_id))))
                for example in item.get("query_examples") or []:
                    if not isinstance(example, dict) or not example.get("proposal_id"):
                        continue
                    query_node = _uri(tenant_id, connection_id, "query_example", example["proposal_id"])
                    graph.add((query_node, RDF.type, KL.QueryExample))
                    graph.add((query_node, PROV.wasGeneratedBy, snapshot_node))
                    graph.add((query_node, PROV.wasDerivedFrom, node))
                    _add_literal(graph, query_node, RDFS.label, example.get("question"))
                    _add_literal(graph, query_node, KL.status, example.get("status"))
                    _add_literal(graph, query_node, KL.executionVerified, False)
            if collection == "concepts":
                graph.add((node, RDF.type, SKOS.Concept))
                _add_literal(graph, node, SKOS.prefLabel, item.get("name"))
                _add_literal(graph, node, SKOS.definition, item.get("definition"))
                _add_literal(graph, node, KL.semanticType, item.get("semantic_type"))
                _add_literal(graph, node, KL.definitionStatus, item.get("definition_status"))
                _add_literal(graph, node, KL.definitionDecidedAt, item.get("definition_decided_at"))
                _add_literal(graph, node, KL.definitionDecisionRationale,
                             item.get("definition_decision_rationale"))
                if item.get("definition_proposal_id"):
                    graph.add((node, KL.definitionProposal, _uri(
                        tenant_id, connection_id, "proposals", str(item["definition_proposal_id"]))))
                for evidence_id in item.get("definition_evidence_ids") or []:
                    graph.add((node, KL.definitionEvidence, _uri(
                        tenant_id, connection_id, "evidence", str(evidence_id))))
            if collection == "evidence":
                _add_literal(graph, node, KL.summary, item.get("summary"))
                asset_id = item.get("asset_id")
                if asset_id:
                    graph.add((node, PROV.wasDerivedFrom, _uri(tenant_id, connection_id, "assets", str(asset_id))))
            if collection == "relationships":
                _add_literal(graph, node, KL.predicateName, item.get("predicate"))
                _add_literal(graph, node, KL.ambiguity, item.get("ambiguity"))
                _add_literal(graph, node, KL.cardinality, item.get("cardinality"))
                _add_literal(graph, node, KL.sourceField, item.get("source_field"))
                _add_literal(graph, node, KL.targetField, item.get("target_field"))
                _add_literal(graph, node, KL.caveat, item.get("caveat"))
                _add_literal(graph, node, KL.relationshipKind, item.get("relationship_kind"))
                _add_literal(graph, node, KL.fromNamespace, item.get("from_namespace"))
                _add_literal(graph, node, KL.toNamespace, item.get("to_namespace"))
                for key, predicate in (("subject_id", KL.subject), ("object_id", KL.object)):
                    if item.get(key):
                        if item.get("assertion_kind") == "term_binding":
                            if key == "object_id":
                                graph.add((node, predicate, _uri(
                                    tenant_id, item["to_connection_id"], "concepts", str(item[key]),
                                )))
                                _add_literal(graph, node, KL.targetSnapshot, item.get("to_snapshot_id"))
                            elif item.get("source_field"):
                                graph.add((node, predicate, _uri(
                                    tenant_id, connection_id, "field", f"{item['source_asset_id']}:{item['source_field']}",
                                )))
                            else:
                                graph.add((node, predicate, _uri(tenant_id, connection_id, "assets", str(item[key]))))
                            continue
                        if key == "object_id" and item.get("predicate") == "cross_source_joins_to":
                            graph.add((node, predicate, _uri(
                                tenant_id, item["to_connection_id"], "assets", str(item[key]),
                            )))
                            _add_literal(graph, node, KL.targetSnapshot, item.get("to_snapshot_id"))
                            continue
                        target_kind = "assets" if str(item[key]) in asset_ids else "concepts"
                        graph.add((node, predicate, _uri(tenant_id, connection_id, target_kind, str(item[key]))))
            if collection == "metrics":
                if item.get("semantics"):
                    _add_literal(graph, node, KL.metricSemantics, json.dumps(item["semantics"], sort_keys=True))
                _add_literal(graph, node, RDFS.comment, item.get("description"))
                _add_literal(graph, node, KL.unit, item.get("unit"))
                _add_literal(graph, node, KL.aggregation, item.get("aggregation"))
                _add_literal(graph, node, KL.certified, item.get("certified", False))
                _measure_binding(graph, node, tenant_id=tenant_id, connection_id=connection_id,
                                 assets=assets_by_id, asset_id=item.get("source_asset_id"),
                                 field_name=item.get("field"))
            if collection == "proposals":
                _add_literal(graph, node, KL.proposalType, item.get("proposal_type"))
                _add_literal(graph, node, KL.proposedDefinition, item.get("proposed_definition"))
                if isinstance(item.get("proposed_mapping"), dict):
                    _add_literal(graph, node, KL.proposedMapping,
                                 json.dumps(item["proposed_mapping"], sort_keys=True))
                _add_literal(graph, node, KL.rationale, item.get("rationale"))
                _add_literal(graph, node, KL.decision, item.get("decision"))
                if item.get("target_concept_id"):
                    graph.add((node, KL.targetConcept, _uri(tenant_id, connection_id, "concepts", str(item["target_concept_id"]))))
                if item.get("proposal_type") == "contract_assertion":
                    _add_literal(graph, node, KL.assertionKind, item.get("assertion_kind"))
                    _add_literal(graph, node, KL.targetAssetId, item.get("asset_id"))
                    if item.get("asset_id") in assets_by_id and item.get("effective_on_current_snapshot") is not False:
                        graph.add((node, KL.targetAsset,
                                   _uri(tenant_id, connection_id, "assets", item["asset_id"])))
                    payload = item.get("payload") or {}
                    # Preserve the validated draft without projecting it as an accepted metric.
                    # A typed JSON literal also retains every contract kind and exact strings.
                    if isinstance(payload, dict):
                        graph.add((node, KL.assertionPayload, Literal(
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            datatype=RDF.JSON)))
                        if item.get("assertion_kind") == "metric":
                            _add_literal(graph, node, RDFS.label, payload.get("name"))
                            _add_literal(graph, node, RDFS.comment, payload.get("description"))
                            _add_literal(graph, node, KL.aggregation, payload.get("aggregation"))
                            _add_literal(graph, node, KL.unit, payload.get("unit"))
                            if item.get("effective_on_current_snapshot") is not False:
                                _measure_binding(
                                    graph, node, tenant_id=tenant_id, connection_id=connection_id,
                                    assets=assets_by_id, asset_id=item.get("asset_id"),
                                    field_name=payload.get("measure_field"))
                for ref in item.get("evidence_refs") or []:
                    from knowledge_layer.inventory_citations import (
                        is_inventory_reference,
                        validate_inventory_reference,
                    )
                    from knowledge_layer.store import KnowledgeError

                    if is_inventory_reference(ref):
                        try:
                            pinned = validate_inventory_reference(ref)
                        except KnowledgeError:
                            continue
                        cid, iid, eid = (pinned[key] for key in ("connection_id", "inventory_id", "evidence_id"))
                        # Opaque entity/proof IDs may contain the separator;
                        # structured encoding keeps distinct exact refs distinct.
                        identity = json.dumps([item_id, cid, iid, pinned["entity_id"], eid, pinned["manifest_digest"]],
                                              ensure_ascii=False, separators=(",", ":"))
                        citation = _uri(tenant_id, connection_id, "inventory-citation", identity)
                        graph.add((citation, RDF.type, KL.InventoryEvidenceCitation))
                        graph.add((citation, PROV.wasGeneratedBy, snapshot_node))
                        graph.add((node, KL.hasEvidenceCitation, citation))
                        graph.add((citation, KL.sourceConnection, _uri(tenant_id, cid, "connection", cid)))
                        graph.add((citation, KL.sourceInventory, _uri(tenant_id, cid, "inventory", iid)))
                        graph.add((citation, KL.sourceEntity, _uri(tenant_id, cid, "inventory-entity", pinned["entity_id"])))
                        graph.add((citation, KL.evidence, _uri(tenant_id, cid, "inventory-evidence", eid)))
                        _add_literal(graph, citation, KL.manifestDigest, pinned["manifest_digest"])
                        _add_literal(graph, citation, KL.sourceTextAuthority, "untrusted_evidence")
                        continue
                    if not isinstance(ref, dict) or not all(
                        isinstance(ref.get(key), str) and ref[key]
                        for key in ("connection_id", "snapshot_id", "evidence_id")
                    ):
                        continue
                    cid, sid, eid = (ref[key] for key in ("connection_id", "snapshot_id", "evidence_id"))
                    citation = _uri(tenant_id, connection_id, "citation", f"{item_id}:{cid}:{sid}:{eid}")
                    graph.add((citation, RDF.type, KL.EvidenceCitation))
                    graph.add((citation, PROV.wasGeneratedBy, snapshot_node))
                    graph.add((node, KL.hasEvidenceCitation, citation))
                    graph.add((citation, KL.sourceConnection, _uri(tenant_id, cid, "connection", cid)))
                    graph.add((citation, KL.sourceSnapshot, _uri(tenant_id, cid, "snapshot", sid)))
                    evidence_node = _uri(tenant_id, cid, "evidence", eid)
                    graph.add((citation, KL.evidence, evidence_node))
                    graph.add((node, PROV.wasDerivedFrom, evidence_node))
            for evidence_id in item.get("evidence_ids") or []:
                graph.add((node, PROV.wasDerivedFrom, _uri(tenant_id, connection_id, "evidence", str(evidence_id))))

    return graph


def snapshot_turtle(snapshot: dict[str, Any], *, tenant_id: str, connection_id: str) -> str:
    return str(project_snapshot(snapshot, tenant_id=tenant_id, connection_id=connection_id).serialize(format="turtle"))
