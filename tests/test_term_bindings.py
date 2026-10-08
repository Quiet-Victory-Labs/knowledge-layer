"""Cross-source term bindings require exact source evidence and two local reviews."""

import json

import httpx
import pytest
from adapter_response_fixture import streamed_response

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.connectors import external_adapter
from knowledge_layer.contracts import semantic_contract
from knowledge_layer.store import KnowledgeConflict, KnowledgeStore, KnowledgeValidationError


@pytest.fixture(params=["document", "native"])
def binding_sources(tmp_path, monkeypatch, request):
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    (warehouse / "orders.csv").write_text("order_id,margin_amount\nO-1,20\n", encoding="utf-8")
    (warehouse / "report.md").write_text("# Margin report\n\nMargin amounts from booked orders.\n", encoding="utf-8")
    documents = tmp_path / "documents"
    documents.mkdir()
    policy = documents / "policy.md"
    policy.write_text(
        "# Retail glossary\n\n## Booked margin\n\nMargin on a booked order excludes canceled orders.\n"
        "\n## Unrelated amount\n\nAn independently scoped amount from a different process.\n", encoding="utf-8",
    )
    native = {
        "id": "margin-17", "kind": "glossary_term", "namespace": "retail",
        "definition": "Margin on a booked order excludes canceled orders.", "reference": "retail/margin-17",
        "version": None, "source_observed_at": None, "owner": None, "authority": None,
        "review_status": "approved", "unit": "USD", "formula": None,
    }
    payload = {"protocol_version": "1.2", "listing_complete": True, "assets": [
        {"name": "Booked margin", "pointer": "retail/margin-17", "asset_type": "semantic_definition",
         "schema": [], "upstream_definition": native},
        {"name": "Booked margin", "pointer": "wholesale/margin-17", "asset_type": "semantic_definition",
         "schema": [], "upstream_definition": {**native, "namespace": "wholesale",
                                                "definition": "Independent wholesale margin scope."}},
    ]}
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TERM_BINDING", json.dumps({
        "endpoint": "https://term-adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["definitions"], "protocol_version": "1.2",
    }))
    real_client = httpx.Client
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, json=payload)), **kwargs,
    ))
    store = KnowledgeStore(database_url="sqlite:///:memory:", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Warehouse", config={"path": "warehouse"})
    doc_source = store.create_connection("demo", kind="local_files", name="Policy", config={
        "path": "documents", "retain_semantic_samples": True,
    })
    imported = store.create_connection("demo", kind="external_adapter", name="Native glossary", config={
        "secret_ref": "KNOWLEDGE_SOURCE_TERM_BINDING", "scope": "definitions",  # pragma: allowlist secret - operator reference
    })
    for cid in (source["id"], doc_source["id"], imported["id"]):
        store.refresh_connection("demo", cid)
    target = doc_source["id"] if request.param == "document" else imported["id"]
    snapshot = store.get_snapshot("demo", target)
    concept = next(item for item in snapshot["concepts"] if item.get("name") == "Booked margin"
                   and (request.param == "document" or item["upstream_definition"]["namespace"] == "retail"))
    table = next(item for item in store.get_snapshot("demo", source["id"])["assets"]
                 if item["asset_type"] == "table")
    try:
        yield {"store": store, "source": source["id"], "target": target, "asset": table["id"],
               "concept": concept["id"], "kind": request.param, "native": native,
               "policy": policy, "warehouse": warehouse, "imported": imported["id"], "payload": payload}
    finally:
        store.close()


def _meaning(fixture):
    store, cid = fixture["store"], fixture["target"]
    snapshot = store.get_snapshot("demo", cid)
    concept = next(item for item in snapshot["concepts"] if item["id"] == fixture["concept"])
    proposal = store.submit_proposal(
        "demo", cid, target_concept_id=concept["id"],
        proposed_definition="Margin on a booked order excludes canceled orders.",
        evidence_ids=concept["evidence_ids"], rationale="A steward reviewed this exact source-local term scope.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", cid, proposal["id"], decision="approve")
    fixture["meaning_proposal"] = proposal["id"]


def _draft(fixture, *, field="margin_amount", relationship="measures", source=None, asset=None,
           change_payload=None, change_refs=None, evidence_ids=None):
    store = fixture["store"]
    cid = source or fixture["source"]
    asset_id = asset or fixture["asset"]
    local = store.get_snapshot("demo", cid)
    target = store.get_snapshot("demo", fixture["target"])
    concept = next(item for item in target["concepts"] if item["id"] == fixture["concept"])
    payload = {"target_connection_id": fixture["target"], "target_snapshot_id": target["id"],
               "target_concept_id": concept["id"], "relationship": relationship}
    if field is not None:
        payload["field"] = field
    ref = {"connection_id": fixture["target"], "snapshot_id": target["id"],
           "evidence_id": concept["evidence_ids"][-1]}
    refs = [ref]
    if change_payload:
        change_payload(payload)
    if change_refs:
        change_refs(refs)
    if evidence_ids is None:
        handle = next(item["id"] for item in local["evidence"] if item.get("asset_id") == asset_id
                      and (item.get("evidence_type") == "asset_metadata" if field is None else
                           (item.get("details") or {}).get("field") == "margin_amount"))
        evidence_ids = [handle]
    return store.submit_contract_assertion(
        "demo", cid, assertion_kind="term_binding", asset_id=asset_id, payload=payload,
        evidence_ids=evidence_ids, evidence_refs=refs, expected_snapshot_id=local["id"],
        rationale="This observed source binding is explicitly related to the separately scoped source term.",
    )


def _approve(fixture):
    _meaning(fixture)
    proposal = _draft(fixture)
    fixture["store"].decide_proposal("demo", fixture["source"], proposal["id"], decision="approve")
    fixture["binding_proposal"] = proposal["id"]
    return proposal


def _assert_suppressed(fixture):
    store, cid = fixture["store"], fixture["source"]
    contract = semantic_contract(store, tenant_id="demo", connection_id=cid)
    assert contract["term_bindings"] == [] and contract["approved_assertions"] == []
    assert len(contract["stale_assertions"]) == 1
    assert contract["stale_assertions"][0]["effective_on_current_snapshot"] is False
    assert not any(item.get("assertion_kind") == "term_binding"
                   for item in store.get_snapshot("demo", cid)["relationships"])


def test_pending_binding_is_readable_without_publication_or_target_review(binding_sources):
    fixture = binding_sources
    proposal = _draft(fixture)
    contract = semantic_contract(fixture["store"], tenant_id="demo", connection_id=fixture["source"])
    assert proposal["status"] == "pending_review"
    assert contract["term_bindings"] == [] and contract["approved_assertions"] == []
    pending, = contract["pending_assertions"]
    assert pending["payload"] == proposal["payload"] and pending["evidence_refs"] == proposal["evidence_refs"]
    assert contract["agent_ready"] is False
    with pytest.raises(KnowledgeConflict):
        fixture["store"].decide_proposal("demo", fixture["source"], proposal["id"], decision="approve")
    assert fixture["store"].get_snapshot("demo", fixture["target"])["agent_ready"] is False


def test_two_reviews_project_exact_typed_non_executable_binding(binding_sources):
    fixture = binding_sources
    proposal = _approve(fixture)
    contract = semantic_contract(fixture["store"], tenant_id="demo", connection_id=fixture["source"])
    edge, = contract["term_bindings"]
    assert edge["predicate"] == "measures" and edge["status"] == "approved"
    assert edge["source_asset_id"] == fixture["asset"] and edge["source_field"] == "margin_amount"
    assert edge["target_concept_id"] == fixture["concept"] and edge["object_id"] == fixture["concept"]
    assert edge["to_connection_id"] == fixture["target"]
    assert edge["to_snapshot_id"] == proposal["payload"]["target_snapshot_id"]
    assert edge["evidence_refs"] == proposal["evidence_refs"] and edge["evidence_ids"] == proposal["evidence_ids"]
    assert edge["decision_proposal_id"] == proposal["id"]
    assert edge["certified"] is False and edge["executable"] is False and contract["agent_ready"] is False
    assert contract["joins"] == [] and contract["cross_source_joins"] == [] and contract["metrics"] == []
    assert "cardinality" not in edge and "formula" not in edge
    assert len(contract["approved_assertions"]) == 1


def test_target_review_revision_requires_a_new_exact_draft_pin(binding_sources):
    fixture = binding_sources
    pending = _draft(fixture)
    _meaning(fixture)
    assert fixture["store"].get_snapshot("demo", fixture["target"])["id"] != pending["payload"]["target_snapshot_id"]
    with pytest.raises(KnowledgeConflict):
        fixture["store"].decide_proposal("demo", fixture["source"], pending["id"], decision="approve")
    replacement = _draft(fixture)
    fixture["store"].decide_proposal("demo", fixture["source"], replacement["id"], decision="approve")
    edge, = semantic_contract(fixture["store"], tenant_id="demo", connection_id=fixture["source"])["term_bindings"]
    assert edge["decision_proposal_id"] == replacement["id"]


@pytest.mark.parametrize("invalidation", ["target_revision", "meaning_archive", "source_revision", "source_field_removed"])
def test_changed_bindings_and_withdrawn_local_meaning_suppress_edge(binding_sources, invalidation):
    fixture = binding_sources
    _approve(fixture)
    store = fixture["store"]
    if invalidation == "target_revision":
        if fixture["kind"] == "native":
            fixture["native"]["definition"] = "Margin includes canceled orders after the revision."
        else:
            fixture["policy"].write_text("# Retail glossary\n\n## Booked margin\n\nMargin now includes canceled orders.\n")
        store.refresh_connection("demo", fixture["target"])
    elif invalidation == "meaning_archive":
        store.archive_proposals("demo", fixture["target"], proposal_ids=[fixture["meaning_proposal"]],
                                reason="The source steward withdrew this exact local meaning review.")
    else:
        header = "order_id,new_margin" if invalidation == "source_field_removed" else "order_id,margin_amount"
        (fixture["warehouse"] / "orders.csv").write_text(f"{header}\nO-1,25\n", encoding="utf-8")
        store.refresh_connection("demo", fixture["source"])
    _assert_suppressed(fixture)


def test_unchanged_source_refresh_preserves_current_evidence_binding(binding_sources):
    fixture = binding_sources
    _approve(fixture)
    fixture["store"].refresh_connection("demo", fixture["source"])
    assert len(semantic_contract(fixture["store"], tenant_id="demo", connection_id=fixture["source"])["term_bindings"]) == 1


def test_removed_source_field_is_suppressed_even_when_old_evidence_handles_remain(binding_sources):
    fixture = binding_sources
    _approve(fixture)
    current = fixture["store"].get_snapshot("demo", fixture["source"])
    asset = next(item for item in current["assets"] if item["id"] == fixture["asset"])
    asset["schema"] = [field for field in asset["schema"] if field["name"] != "margin_amount"]
    fixture["store"].save_snapshot("demo", fixture["source"], {**current, "snapshot_id": "removed-field-same-evidence"})
    _assert_suppressed(fixture)


@pytest.mark.parametrize("revoked", ["source", "target"])
def test_each_source_grant_is_required_and_target_denial_hides_identity(binding_sources, revoked):
    fixture = binding_sources
    _approve(fixture)
    store = fixture["store"]
    for cid in (fixture["source"], fixture["target"]):
        store.grant_source_access("demo", cid, principal_type="user", principal_id="reader", role="read")
    reader = Principal("reader", frozenset(), frozenset())
    store.access_enforced = True
    with principal_scope(reader):
        assert semantic_contract(store, tenant_id="demo", connection_id=fixture["source"])["term_bindings"]
    store.access_enforced = False
    store.revoke_source_access("demo", fixture[revoked], principal_type="user", principal_id="reader")
    store.access_enforced = True
    with principal_scope(reader):
        if revoked == "source":
            with pytest.raises(ValueError, match="not found"):
                semantic_contract(store, tenant_id="demo", connection_id=fixture["source"])
        else:
            contract = semantic_contract(store, tenant_id="demo", connection_id=fixture["source"])
            assert contract["term_bindings"] == [] and contract["approved_assertions"] == []
            assert contract["pending_assertions"] == [] and contract["stale_assertions"] == []
            assert fixture["target"] not in json.dumps(contract) and fixture["concept"] not in json.dumps(contract)
            assert store.get_snapshot("demo", fixture["source"])["dependency_unavailable"] is True


@pytest.mark.parametrize("invalid", ["field", "metadata", "predicate", "extra_key", "target_concept", "target_evidence", "missing_ref", "partial_ref", "snapshot"])
def test_binding_fields_metadata_and_target_refs_are_strict(binding_sources, invalid):
    fixture = binding_sources
    kwargs = {}
    if invalid == "field":
        kwargs["field"] = "unobserved_field"
    elif invalid == "metadata":
        snapshot = fixture["store"].get_snapshot("demo", fixture["source"])
        kwargs["evidence_ids"] = [next(item["id"] for item in snapshot["evidence"]
                                       if item.get("asset_id") == fixture["asset"] and item["evidence_type"] == "asset_metadata")]
    elif invalid == "predicate":
        kwargs["relationship"] = "same_as"
    elif invalid == "extra_key":
        kwargs["change_payload"] = lambda payload: payload.update({"cardinality": "one_to_one"})
    elif invalid == "target_concept":
        kwargs["change_payload"] = lambda payload: payload.update({"target_concept_id": "unknown-concept"})
    elif invalid == "target_evidence":
        target = fixture["store"].get_snapshot("demo", fixture["target"])
        other = next(item for item in target["concepts"] if item["id"] != fixture["concept"]
                     and item.get("semantic_type") in {"document_business_term", "upstream_glossary_term"})
        kwargs["change_refs"] = lambda refs: refs[0].update({"evidence_id": other["evidence_ids"][0]})
    elif invalid == "missing_ref":
        kwargs["change_refs"] = lambda refs: refs.clear()
    elif invalid == "partial_ref":
        kwargs["change_refs"] = lambda refs: refs[0].pop("snapshot_id")
    else:
        kwargs["change_payload"] = lambda payload: payload.update({"target_snapshot_id": "old-snapshot"})
    with pytest.raises((KnowledgeValidationError, KnowledgeConflict)):
        _draft(fixture, **kwargs)


@pytest.mark.parametrize("asset_kind", ["table", "document", "semantic_definition"])
def test_asset_level_bindings_require_asset_metadata_for_each_supported_source_type(binding_sources, asset_kind):
    fixture = binding_sources
    _meaning(fixture)
    cid = fixture["source"]
    if asset_kind == "semantic_definition":
        # A separate authorized import is a source asset; the target remains
        # independently scoped even when both sources expose native terms.
        source = fixture["store"].create_connection("demo", kind="external_adapter", name="Other native source", config={
            "secret_ref": "KNOWLEDGE_SOURCE_TERM_BINDING", "scope": "definitions",  # pragma: allowlist secret - operator reference
        })
        cid = source["id"]
        fixture["store"].refresh_connection("demo", cid)
    snapshot = fixture["store"].get_snapshot("demo", cid)
    asset = next(item for item in snapshot["assets"] if item["asset_type"] == asset_kind)
    proposal = _draft(fixture, field=None, relationship="mentions", source=cid, asset=asset["id"])
    fixture["store"].decide_proposal("demo", cid, proposal["id"], decision="approve")
    edge, = semantic_contract(fixture["store"], tenant_id="demo", connection_id=cid)["term_bindings"]
    assert edge["source_asset_id"] == asset["id"] and edge["source_field"] is None
    assert edge["predicate"] == "mentions"
