"""Selected native observations support local reviews without inheriting authority."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select, update

from knowledge_layer import federated
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.federated import (
    _current_document_citations,
    cited_glossary,
    cited_inventory_evidence,
    cited_upstream_definitions,
    federated_evidence_graph,
)
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.source_policy import IDENTITY_FIELDS, SourcePolicy
from knowledge_layer.store import (
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeStore,
)

OWNER = Principal("owner", frozenset({"stewards"}), frozenset({"knowledge.sources.manage"}))
READER = Principal("reader", frozenset())
NATIVE_TEXT = "Native-only wording is opaque rather than a company instruction.\n" * 300


def _complete(fixture, *, native=None, entities=None):
    inventory, cid = fixture["inventory"], fixture["catalog"]
    native = native if native is not None else {"definition": NATIVE_TEXT, "unit": None,
        "approval": "approved", "owners": ["Native owner"], "formula": "unverified_formula()",
        "policy": {"read": True}, "relationships": [{"id": "unresolved"}],
        "unknown": None, "zero": 0, "empty": "", "disabled": False}
    entity = {"id": "entity.margin", "kind": "semantic_definition",
              "payload": {"name": "Booked margin", "native_id": "margin.1", "namespace": "finance"},
              "evidence_ids": ["proof.margin"]}
    proof = {"id": "proof.margin", "kind": "catalog_native_record", "payload": {"native_record": native}}
    run = inventory.begin("demo", cid, scope={"namespace": "finance", "selectors": ["terms"]},
        policy={"id": "operator-capture", "revision": None}, watermark={"kind": "unknown", "value": None},
        limits={"max_pages": 10, "max_entities": 10, "max_evidence": 10, "max_total_bytes": 256 * 1024})
    for index, (page_entities, proofs, cursor, next_cursor) in enumerate([
        ([entity] if entities is None else entities, [], None, "later-proof"),
        ([], [proof, {"id": "proof.unlinked", "kind": "catalog_native_record", "payload": {"owner": None}}],
         "later-proof", None),
    ]):
        run = inventory.append("demo", cid, run["id"], page_index=index, cursor=cursor,
            next_cursor=next_cursor, entities=page_entities, evidence=proofs,
            scope_digest=run["scope_digest"], policy_digest=run["policy_digest"], watermark_digest=run["watermark_digest"])
    return inventory.commit("demo", cid, run["id"], completion={"listing_complete": True,
        "pages": run["page_count"], "entities": run["entity_count"], "evidence": run["evidence_count"],
        "manifest_digest": run["manifest_digest"]})


@pytest.fixture
def cited_inventory(tmp_path):
    for name in ("warehouse", "catalog", "policy"):
        (tmp_path / name).mkdir()
    (tmp_path / "warehouse" / "orders.csv").write_text("order_id,margin_amount\nO-1,20\n", encoding="utf-8")
    policy_path = tmp_path / "policy" / "policy.md"
    policy_path.write_text("# Policy\n\n## Booked margin\n\nRefunds are excluded from booked margin.\n", encoding="utf-8")
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    sources = {name: store.create_connection("demo", kind="local_files", name=name, config={"path": name,
        **({"retain_semantic_samples": True} if name == "policy" else {})}) for name in ("warehouse", "catalog", "policy")}
    local = store.refresh_connection("demo", sources["warehouse"]["id"])["snapshot"]
    docs = store.refresh_connection("demo", sources["policy"]["id"])["snapshot"]
    term = next(item for item in docs["concepts"] if item.get("name") == "Booked margin")
    meaning = store.submit_proposal("demo", sources["policy"]["id"], target_concept_id=term["id"],
        proposed_definition="Refunds are excluded from booked margin.", evidence_ids=term["evidence_ids"],
        rationale="The policy owner reviewed this exact documentary definition.", expected_snapshot_id=docs["id"])
    store.decide_proposal("demo", sources["policy"]["id"], meaning["id"], decision="approve")
    docs = store.get_snapshot("demo", sources["policy"]["id"])
    for source in sources.values():
        for subject, role in (("owner", "manage"), ("reader", "read")):
            store.grant_source_access("demo", source["id"], principal_type="user", principal_id=subject, role=role)
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": source["kind"],
        "selector": {key: value for key, value in source["config"].items() if key in IDENTITY_FIELDS[source["kind"]]},
        "allowed_subjects": ["owner", "reader"], "allowed_groups": [],
    } for source in sources.values()]}))
    store.source_admin_group = "stewards"
    store.access_enforced = True
    asset = local["assets"][0]
    fixture = {"store": store, "inventory": InventoryStore(store),
        **{name: source["id"] for name, source in sources.items()}, "asset": asset["id"],
        "handle": next(item["id"] for item in local["evidence"] if item.get("asset_id") == asset["id"]
                       and (item.get("details") or {}).get("field") == "margin_amount"),
        "doc_ref": {"connection_id": sources["policy"]["id"], "snapshot_id": docs["id"],
                    "evidence_id": term["evidence_ids"][0]}, "policy_path": policy_path}
    with principal_scope(OWNER):
        fixture["manifest"] = _complete(fixture)
    fixture["ref"] = {"connection_id": fixture["catalog"], "inventory_id": fixture["manifest"]["id"],
        "manifest_digest": fixture["manifest"]["manifest_digest"], "entity_id": "entity.margin", "evidence_id": "proof.margin"}
    try:
        yield fixture
    finally:
        store.close()


def _propose(fixture, *, approve=True, refs=None):
    store, cid = fixture["store"], fixture["warehouse"]
    with principal_scope(OWNER):
        snapshot = store.get_snapshot("demo", cid)
        proposal = store.submit_contract_assertion("demo", cid, assertion_kind="field_description", asset_id=fixture["asset"],
            payload={"field": "margin_amount", "description": "Booked order margin excludes refunds."},
            evidence_ids=[fixture["handle"]], evidence_refs=refs if refs is not None else [fixture["ref"]],
            rationale="A local source owner independently interprets the exact captured native observation.",
            expected_snapshot_id=snapshot["id"])
        if approve:
            store.decide_proposal("demo", cid, proposal["id"], decision="approve")
    return proposal


def _citations(fixture):
    return cited_inventory_evidence(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"])


def test_raw_native_approval_never_self_approves_or_copies_full_payload(cited_inventory):
    fixture = cited_inventory
    proposal = _propose(fixture, approve=False)
    with principal_scope(READER):
        assert _citations(fixture) == []
        pending_graph = federated_evidence_graph(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"])
        assert {node["status"] for node in pending_graph["nodes"] if node["kind"].startswith("inventory_")} == {"observed"}
    with principal_scope(OWNER):
        fixture["store"].decide_proposal("demo", fixture["warehouse"], proposal["id"], decision="approve")
    with principal_scope(READER):
        entry, = _citations(fixture)
        assert entry["evidence_ref"] == fixture["ref"]
        assert entry["entity_id"] == "entity.margin" and entry["entity_kind"] == "semantic_definition"
        assert len(entry["entity_digest"]) == len(entry["evidence_digest"]) == 64
        assert entry["native_authority"] == entry["source_text_authority"] == "untrusted_evidence"
        assert entry["meaning_certified"] is False and entry["locally_executable"] is False
        assert entry["source_assertion"]["assertion_id"] == proposal["id"]
        assert entry["source_assertion"]["status"] == "approved"
        assert NATIVE_TEXT not in json.dumps(entry)
        assert "native_record" not in entry and "snapshot_id" not in entry
        assert cited_glossary(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"]) == []
        assert cited_upstream_definitions(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"]) == []
        selected = fixture["inventory"].select("demo", fixture["catalog"], fixture["manifest"]["id"],
            expected_manifest_digest=fixture["manifest"]["manifest_digest"], entity_ids=["entity.margin"])
        assert selected["evidence"][0]["payload"]["native_record"]["definition"] == NATIVE_TEXT


def test_graph_preserves_typed_inventory_pins_without_snapshot_or_join_equivalence(cited_inventory):
    fixture = cited_inventory
    _propose(fixture)
    with principal_scope(READER):
        graph = federated_evidence_graph(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"])
    nodes = [item for item in graph["nodes"] if item["kind"].startswith("inventory_")]
    assert {node["kind"] for node in nodes} == {"inventory_entity", "inventory_evidence"}
    assert all(node["inventory_id"] == fixture["manifest"]["id"] and "snapshot_id" not in node for node in nodes)
    assert all(node["manifest_digest"] == fixture["manifest"]["manifest_digest"] for node in nodes)
    assert all(node["trust_state"] == "untrusted_evidence" and not node["meaning_certified"] for node in nodes)
    assert next(node for node in nodes if node["kind"] == "inventory_evidence")["evidence_ref"] == fixture["ref"]
    related = [edge for edge in graph["edges"] if any(edge[key] in {node["id"] for node in nodes}
                                                     for key in ("subject_id", "object_id"))]
    assert {edge["predicate"] for edge in related} == {"cites_inventory_evidence", "evidence_for_inventory_entity"}
    assert all(edge["status"] == "observed" and edge["confidence"] is None for edge in related)
    assert NATIVE_TEXT not in json.dumps(graph)


def test_local_assertion_can_be_published_without_publishing_inventory(cited_inventory):
    fixture = cited_inventory
    _propose(fixture)
    store = fixture["store"]
    with principal_scope(OWNER):
        snapshot = store.get_snapshot("demo", fixture["warehouse"])
        assert store.publication_preview("demo", fixture["warehouse"])["eligible"]
        store.publish_source("demo", fixture["warehouse"], expected_snapshot_id=snapshot["id"],
            expected_snapshot_version=snapshot["version"], rationale="The source owner reviewed this local interpretation.")
    with principal_scope(READER):
        contract = semantic_contract(store, tenant_id="demo", connection_id=fixture["warehouse"])
        assert contract["cited_inventory_evidence"][0]["evidence_ref"] == fixture["ref"]
        assert store.get_snapshot("demo", fixture["catalog"]) is None
        catalog = store.get_connection("demo", fixture["catalog"])
        assert not catalog["agent_ready"] and catalog["last_complete_refresh_at"] is None
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.publications).where(
            store.publications.c.connection_id == fixture["catalog"])).scalar_one() == 0


@pytest.mark.parametrize("invalidation", ["successor", "empty_successor", "source_error", "grant", "policy", "corrupt_proof"])
def test_each_inventory_invalidation_suppresses_reviewed_citations(cited_inventory, invalidation):
    fixture = cited_inventory
    _propose(fixture)
    store, cid = fixture["store"], fixture["catalog"]
    with principal_scope(READER):
        assert _citations(fixture)
    if invalidation == "successor":
        with principal_scope(OWNER):
            _complete(fixture, native={"definition": "A later captured native definition."})
    elif invalidation == "empty_successor":
        with principal_scope(OWNER):
            inventory = fixture["inventory"]
            run = inventory.begin("demo", cid, scope={"namespace": "finance", "selectors": ["terms"]},
                policy={"id": "operator-capture", "revision": None}, watermark={"kind": "unknown", "value": None},
                limits={"max_pages": 10, "max_entities": 10, "max_evidence": 10, "max_total_bytes": 256 * 1024})
            run = inventory.append("demo", cid, run["id"], page_index=0, cursor=None, next_cursor=None,
                entities=[], evidence=[], scope_digest=run["scope_digest"], policy_digest=run["policy_digest"],
                watermark_digest=run["watermark_digest"])
            inventory.commit("demo", cid, run["id"], completion={"listing_complete": True,
                "pages": 1, "entities": 0, "evidence": 0, "manifest_digest": run["manifest_digest"]})
    elif invalidation == "source_error":
        with store.engine.begin() as conn:
            conn.execute(update(store.connections).where(store.connections.c.id == cid).values(status="error"))
    elif invalidation == "grant":
        store.access_enforced = False
        store.revoke_source_access("demo", cid, principal_type="user", principal_id="reader")
        store.access_enforced = True
    elif invalidation == "policy":
        store.source_policy = SourcePolicy(tuple(approval for approval in store.source_policy.approvals
                                                if "catalog" not in approval.identity))
    else:
        with store.engine.begin() as conn:
            conn.execute(update(store.inventory_evidence).where(store.inventory_evidence.c.inventory_id == fixture["manifest"]["id"],
                store.inventory_evidence.c.id == "proof.margin").values(payload={"replacement": "not-the-record-digest"}))
    with principal_scope(READER):
        assert _citations(fixture) == []
        graph = federated_evidence_graph(store, tenant_id="demo", connection_id=fixture["warehouse"])
        assert not any(node["kind"].startswith("inventory_") for node in graph["nodes"])
        assert "Native owner" not in json.dumps(graph)


@pytest.mark.parametrize("changed_source", ["inventory", "document"])
@pytest.mark.parametrize("inventory_first", [True, False])
def test_mixed_document_and_inventory_refs_are_all_or_nothing(cited_inventory, changed_source, inventory_first):
    fixture = cited_inventory
    refs = [fixture["ref"], fixture["doc_ref"]] if inventory_first else [fixture["doc_ref"], fixture["ref"]]
    _propose(fixture, refs=refs)
    store = fixture["store"]
    with principal_scope(READER):
        assert _citations(fixture)
        assert cited_glossary(store, tenant_id="demo", connection_id=fixture["warehouse"])
    if changed_source == "inventory":
        with principal_scope(OWNER):
            _complete(fixture, native={"definition": "Revised captured term."})
    else:
        fixture["policy_path"].write_text("# Policy\n\n## Booked margin\n\nThe documentary meaning changed.\n", encoding="utf-8")
        with principal_scope(OWNER):
            store.refresh_connection("demo", fixture["policy"])
    with principal_scope(READER):
        assert _citations(fixture) == []
        assert cited_glossary(store, tenant_id="demo", connection_id=fixture["warehouse"]) == []
        assert cited_upstream_definitions(store, tenant_id="demo", connection_id=fixture["warehouse"]) == []
        graph = federated_evidence_graph(store, tenant_id="demo", connection_id=fixture["warehouse"])
        assert not any(node["connection_id"] != fixture["warehouse"] for node in graph["nodes"])


def test_graph_retains_each_entity_binding_when_native_evidence_is_shared(cited_inventory):
    fixture = cited_inventory
    entities = [{"id": identifier, "kind": "semantic_definition", "payload": {"name": "Margin"},
                 "evidence_ids": ["proof.margin"]} for identifier in ("entity.retail", "entity.wholesale")]
    with principal_scope(OWNER):
        run = _complete(fixture, entities=entities)
    refs = [{**fixture["ref"], "inventory_id": run["id"], "manifest_digest": run["manifest_digest"],
             "entity_id": item["id"]} for item in entities]
    _propose(fixture, refs=refs)
    with principal_scope(READER):
        entries = _citations(fixture)
        graph = federated_evidence_graph(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"])
    assert {entry["entity_id"] for entry in entries} == {"entity.retail", "entity.wholesale"}
    proofs = [node for node in graph["nodes"] if node["kind"] == "inventory_evidence"]
    assert len(proofs) == 2 and len({node["id"] for node in proofs}) == 2
    assert {node["evidence_ref"]["entity_id"] for node in proofs} == {"entity.retail", "entity.wholesale"}
    assert all(node["evidence_ref"]["evidence_id"] == "proof.margin" for node in proofs)


@pytest.mark.parametrize("fault", ["unlinked", "manifest", "mixed_keys", "target_source"])
def test_invalid_inventory_pins_refuse_without_a_new_local_proposal(cited_inventory, fault):
    fixture = cited_inventory
    ref = deepcopy(fixture["ref"])
    if fault == "unlinked":
        ref["evidence_id"] = "proof.unlinked"
    elif fault == "manifest":
        ref["manifest_digest"] = "0" * 64
    elif fault == "mixed_keys":
        ref["snapshot_id"] = "invented-snapshot"
    else:
        ref["connection_id"] = fixture["warehouse"]
    with fixture["store"].engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(fixture["store"].contract_proposals)).scalar_one()
    with pytest.raises(KnowledgeError):
        _propose(fixture, refs=[ref])
    with fixture["store"].engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(fixture["store"].contract_proposals)).scalar_one() == before


def test_inventory_refs_never_establish_cross_source_joins(cited_inventory):
    fixture = cited_inventory
    store = fixture["store"]
    with principal_scope(OWNER):
        snapshot = store.get_snapshot("demo", fixture["warehouse"])
        with pytest.raises(KnowledgeError):
            store.submit_contract_assertion("demo", fixture["warehouse"], assertion_kind="cross_source_join",
                asset_id=fixture["asset"], payload={}, evidence_ids=[fixture["handle"]], evidence_refs=[fixture["ref"]],
                rationale="Native relationship metadata is not a verified structural join.", expected_snapshot_id=snapshot["id"])
        assert _current_document_citations(store, tenant_id="demo", connection_id=fixture["warehouse"],
            proposal={"assertion_kind": "cross_source_join", "evidence_refs": [fixture["ref"]],
                      "evidence_ids": [fixture["handle"]]},
            evidence={item["id"]: item for item in snapshot["evidence"]}, documents={}) is None


def test_aggregate_inventory_budget_refuses_instead_of_silently_omitting_citations(cited_inventory, monkeypatch):
    from knowledge_layer import inventory_citations

    fixture = cited_inventory
    with principal_scope(READER):
        snapshot = fixture["store"].get_snapshot("demo", fixture["warehouse"])
        def exhausted(*_args, **_kwargs):
            raise inventory_citations.InventoryCitationBudgetExceeded("Inventory citation operation exceeded its bounded budget")
        monkeypatch.setattr(inventory_citations, "resolve_inventory_citation", exhausted)
        with pytest.raises(inventory_citations.InventoryCitationBudgetExceeded):
            _current_document_citations(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"],
                proposal={"assertion_kind": "field_description", "evidence_refs": [fixture["ref"]],
                          "evidence_ids": [fixture["handle"]]},
                evidence={item["id"]: item for item in snapshot["evidence"]}, documents={})


def test_wrong_snapshot_and_missing_target_return_no_citation(cited_inventory):
    fixture = cited_inventory
    _propose(fixture)
    with principal_scope(READER):
        assert cited_inventory_evidence(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"],
                                       expected_snapshot_id="old-snapshot") == []
        assert cited_inventory_evidence(fixture["store"], tenant_id="demo", connection_id="unavailable-source") == []
        assert cited_inventory_evidence(fixture["store"], tenant_id="demo", connection_id=fixture["catalog"]) == []


def test_denied_target_grant_exposes_neither_inventory_handles_nor_graph(cited_inventory):
    fixture = cited_inventory
    _propose(fixture)
    store = fixture["store"]
    store.access_enforced = False
    store.revoke_source_access("demo", fixture["warehouse"], principal_type="user", principal_id="reader")
    store.access_enforced = True
    with principal_scope(READER):
        assert _citations(fixture) == []
        with pytest.raises(KnowledgeNotFound, match="Source snapshot was not found"):
            federated_evidence_graph(store, tenant_id="demo", connection_id=fixture["warehouse"])


@pytest.mark.parametrize("operation", ["citations", "graph"])
def test_withdrawal_during_later_proposal_assembly_cannot_return_earlier_handles(cited_inventory, monkeypatch, operation):
    fixture = cited_inventory
    _propose(fixture)
    _propose(fixture)
    original = federated._current_document_citations
    calls = 0
    def withdraw_after_resolution(*args, **kwargs):
        nonlocal calls
        resolved = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            store = fixture["store"]
            store.access_enforced = False
            store.revoke_source_access("demo", fixture["catalog"], principal_type="user", principal_id="reader")
            store.access_enforced = True
        return resolved
    monkeypatch.setattr(federated, "_current_document_citations", withdraw_after_resolution)
    with principal_scope(READER):
        if operation == "citations":
            assert _citations(fixture) == []
        else:
            with pytest.raises(KnowledgeConflict, match="Cited sources changed during graph assembly"):
                federated_evidence_graph(fixture["store"], tenant_id="demo", connection_id=fixture["warehouse"])
    assert calls >= 3


def test_selected_inventory_helpers_read_registry_without_legacy_snapshot_or_writes(cited_inventory, monkeypatch):
    fixture = cited_inventory
    _propose(fixture)
    store = fixture["store"]
    original = store.get_connection
    def bounded_source(tenant, source_id):
        if source_id == fixture["catalog"]:
            pytest.fail("Inventory citations materialized a legacy source connection projection")
        return original(tenant, source_id)
    monkeypatch.setattr(store, "get_connection", bounded_source)
    before = {}
    with store.engine.connect() as conn:
        for table in (store.snapshots, store.publications, store.audit_events, store.contract_proposals):
            before[table.name] = conn.execute(select(func.count()).select_from(table)).scalar_one()
    with principal_scope(READER):
        assert _citations(fixture)
        assert federated_evidence_graph(store, tenant_id="demo", connection_id=fixture["warehouse"])["linked_assertions"] == 1
    with store.engine.connect() as conn:
        for table in (store.snapshots, store.publications, store.audit_events, store.contract_proposals):
            assert conn.execute(select(func.count()).select_from(table)).scalar_one() == before[table.name]
