"""Actual snapshotless local meaning, governance and invalidation effects."""

import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select, update
from test_inventory_citations import commit_catalog, setup_case

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory_concepts import InventoryConcepts
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeValidationError,
)


@pytest.fixture
def case(tmp_path):
    fixture = setup_case(tmp_path)
    fixture["concepts"] = InventoryConcepts(fixture["store"])
    yield fixture
    fixture["store"].close()


def draft(case, **kwargs):
    return case["concepts"].propose("demo", case["catalog"], **{
        "evidence_ref": deepcopy(case["ref"]), "name": "Net revenue",
        "definition": "Finance uses net revenue after refunded amounts, in USD.",
        "concept_kind": "business_term",
        "rationale": "The owner interprets this catalog observation within the finance scope.", **kwargs})


def page(case, **kwargs):
    return case["concepts"].page("demo", case["catalog"], **{
        "inventory_id": case["ref"]["inventory_id"],
        "expected_manifest_digest": case["ref"]["manifest_digest"], **kwargs})


def publish(case, proposal=None):
    proposal = proposal or draft(case)
    case["store"].decide_proposal("demo", case["catalog"], proposal["id"], decision="approve",
                                  rationale="The source owner reviewed the local business definition.")
    return case["concepts"].publish("demo", case["catalog"], concept_id=proposal["id"],
        expected_proposal_digest=proposal["proposal_digest"],
        rationale="The steward accepts this reviewed finance definition for scoped use.")


def count(store, table):
    with store.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_catalog_only_concept_is_consumable_after_review_and_steward_publication(case, monkeypatch):
    store, concepts, cid = case["store"], case["concepts"], case["catalog"]
    clocks = store.get_connection("demo", cid)
    proposal = draft(case)
    assert proposal["definition_authority"] == "unreviewed_proposal"
    assert page(case)["items"] == []
    assert page(case, include_drafts=True)["items"][0]["id"] == proposal["id"]
    assert store.get_context("demo", concept_id=proposal["id"]) is None
    with pytest.raises(KnowledgeConflict, match="owner approval"):
        concepts.publish("demo", cid, concept_id=proposal["id"],
                         expected_proposal_digest=proposal["proposal_digest"], rationale="Reviewed by nobody yet.")
    store.decide_proposal("demo", cid, proposal["id"], decision="approve")
    assert store.get_context("demo", concept_id=proposal["id"]) is None
    published = publish(case, proposal)
    assert published["publication_state"] == "published"
    assert published["definition_authority"] == "locally_reviewed"
    assert published["native_meaning_certified"] is False and published["locally_executable"] is False
    assert published["native_authority"] == "untrusted_evidence"
    monkeypatch.setattr(store, "get_concepts", lambda *a, **k: pytest.fail("inventory ID scanned legacy concepts"))
    context = store.get_context("demo", concept_id=proposal["id"], connection_id=cid)
    assert context["basis"] == "inventory" and "snapshot_id" not in context and "snapshot" not in context
    assert context["concept"]["definition"] == proposal["concept"]["definition"]
    assert context["evidence_ref"] == case["ref"] and not context["source_reads_executed"]
    assert store.get_concept("demo", proposal["id"])["id"] == proposal["id"]
    assert store.get_context("demo", concept_id=proposal["id"], connection_id=case["target"]) is None
    assert page(case, query="net revenue")["items"][0]["id"] == proposal["id"]
    assert page(case, query="unknown")["items"] == []
    assert store.get_snapshot("demo", cid) is None and not store.get_connection("demo", cid)["agent_ready"]
    assert count(store, store.snapshots) == 1
    current = store.get_connection("demo", cid)
    assert {key: clocks[key] for key in clocks if key.startswith("last_")} == {
        key: current[key] for key in current if key.startswith("last_")}


@pytest.mark.parametrize("change", ["successor", "empty", "error", "config"])
def test_current_head_outage_and_identity_changes_refuse_published_meaning(case, change):
    published = publish(case)
    if change in {"successor", "empty"}:
        commit_catalog(case, definition="Finance changed its definition.", empty=change == "empty")
    elif change == "error":
        with case["store"].engine.begin() as conn:
            conn.execute(update(case["store"].connections).where(case["store"].connections.c.id == case["catalog"])
                         .values(status="error"))
    else:
        case["store"].update_connection_config("demo", case["catalog"], {"path": "another_catalog"})
    with pytest.raises(KnowledgeConflict):
        case["concepts"].get("demo", case["catalog"], concept_id=published["id"])
    with pytest.raises(KnowledgeConflict):
        case["concepts"].publish("demo", case["catalog"], concept_id=published["id"],
            expected_proposal_digest=published["proposal_digest"], rationale="Old evidence must not be published again.")


@pytest.mark.parametrize("field,value", [("manifest_digest", "f" * 64), ("inventory_id", "missing"),
    ("entity_id", "missing"), ("evidence_id", "missing"), ("connection_id", "missing"), ("snapshot_id", "fake")])
def test_bad_inventory_pins_never_create_a_proposal(case, field, value):
    with pytest.raises(KnowledgeError):
        draft(case, evidence_ref={**case["ref"], field: value})
    assert count(case["store"], case["store"].inventory_concept_proposals) == 0


@pytest.mark.parametrize("field,value", [("name", ""), ("definition", ""), ("definition", "x" * 4001),
    ("concept_kind", "executable_metric"), ("concept_kind", {}), ("rationale", "short")])
def test_no_missing_meaning_or_native_execution_authority_can_be_supplied(case, field, value):
    with pytest.raises(KnowledgeValidationError):
        draft(case, **{field: value})


def test_tampered_local_text_proof_and_decision_digest_never_gain_authority(case):
    published = publish(case)
    store = case["store"]
    with store.engine.begin() as conn:
        conn.execute(update(store.decisions).where(store.decisions.c.proposal_id == published["id"])
                     .values(proposal_digest="0" * 64))
    assert page(case)["items"] == []
    with pytest.raises(KnowledgeNotFound):
        case["concepts"].get("demo", case["catalog"], concept_id=published["id"])
    with store.engine.begin() as conn:
        conn.execute(update(store.inventory_concept_proposals).where(store.inventory_concept_proposals.c.id == published["id"])
                     .values(definition="Attacker changed local meaning without a new review."))
    with pytest.raises(KnowledgeConflict, match="integrity"):
        page(case, include_drafts=True)


def test_withdrawal_and_archive_remove_effective_meaning_and_retain_audit(case):
    published = publish(case)
    result = case["concepts"].withdraw("demo", case["catalog"], concept_id=published["id"],
                                      reason="The owner withdrew this definition.")
    assert "concept" not in result and result["publication_state"] == "withdrawn"
    assert page(case)["items"] == []
    with pytest.raises(KnowledgeConflict, match="withdrawn"):
        case["concepts"].publish("demo", case["catalog"], concept_id=published["id"],
            expected_proposal_digest=published["proposal_digest"], rationale="This cannot restore withdrawn meaning.")
    second = publish(case)
    archived = case["store"].archive_proposals("demo", case["catalog"], proposal_ids=[second["id"]],
                                              reason="Superseded reviewed definition needs retirement.")
    assert archived["publication_withdrawn"] and archived["archived_count"] == 1
    assert page(case)["items"] == []
    assert case["store"].get_context("demo", concept_id=second["id"]) is None
    assert count(case["store"], case["store"].decisions) == 2
    with case["store"].engine.connect() as conn:
        events = set(conn.execute(select(case["store"].audit_events.c.event_type)).scalars())
    assert {"proposal.submit", "proposal.decide", "inventory_concept.publish", "inventory_concept.withdraw", "proposal.archive"} <= events


def test_duplicate_labels_and_scoped_cursors_preserve_distinct_local_meaning(case):
    first = publish(case)
    second = publish(case, draft(case, definition="Bookings defines net revenue on a different business basis."))
    assert first["id"] != second["id"]
    result = page(case, limit=1)
    next_result = page(case, limit=1, after=result["next_cursor"])
    assert {result["items"][0]["id"], next_result["items"][0]["id"]} == {first["id"], second["id"]}
    assert next_result["next_cursor"] is None
    assert result["items"][0]["cross_source_candidates"] == []
    with pytest.raises(KnowledgeValidationError, match="cursor"):
        page(case, limit=1, after=result["next_cursor"], query="revenue")
    for limit in (0, 21, True):
        with pytest.raises(KnowledgeValidationError):
            page(case, limit=limit)


def configure_access(case):
    store, cid = case["store"], case["catalog"]
    for subject, role in (("owner", "manage"), ("manager", "manage"), ("reader", "read")):
        store.grant_source_access("demo", cid, principal_type="user", principal_id=subject, role=role)
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": "catalog", "include": "**/*"},
        "allowed_subjects": ["owner", "manager", "reader"], "allowed_groups": [],
    }]}))
    store.source_admin_group = "stewards"
    store.access_enforced = True
    with principal_scope(Principal("owner", frozenset({"stewards"}))):
        commit_catalog(case)
    return (Principal("owner", frozenset({"stewards"})),
            Principal("manager", frozenset(), frozenset({"knowledge.sources.manage"})),
            Principal("reader", frozenset()))


def test_contributor_proposes_owner_reviews_steward_publishes_reader_consumes(case):
    owner, manager, reader = configure_access(case)
    with principal_scope(reader):
        proposal = draft(case)
        with pytest.raises(KnowledgeAccessDenied):
            page(case, include_drafts=True)
        with pytest.raises(KnowledgeAccessDenied):
            case["concepts"].decide("demo", case["catalog"], concept_id=proposal["id"], decision="approve")
    with principal_scope(manager):
        case["concepts"].decide("demo", case["catalog"], concept_id=proposal["id"], decision="approve")
        with pytest.raises(KnowledgeAccessDenied, match="steward"):
            case["concepts"].publish("demo", case["catalog"], concept_id=proposal["id"],
                expected_proposal_digest=proposal["proposal_digest"], rationale="Managers cannot steward-publish alone.")
    with principal_scope(owner):
        publish(case, proposal)
    with principal_scope(reader):
        assert page(case)["items"][0]["id"] == proposal["id"]
    with principal_scope(Principal("stranger", frozenset())):
        assert case["store"].get_context("demo", concept_id=proposal["id"]) is None
        with pytest.raises(KnowledgeNotFound):
            page(case)
    case["store"].access_enforced = False
    try:
        case["store"].revoke_source_access("demo", case["catalog"], principal_type="user", principal_id="reader")
    finally:
        case["store"].access_enforced = True
    with principal_scope(reader):
        assert case["store"].get_context("demo", concept_id=proposal["id"]) is None
    case["store"].source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": []}))
    with principal_scope(owner):
        with pytest.raises(KnowledgeAccessDenied):
            case["concepts"].get("demo", case["catalog"], concept_id=proposal["id"])
        assert case["concepts"].withdraw("demo", case["catalog"], concept_id=proposal["id"],
                                         reason="Policy withdrawal must still allow retirement.")["publication_state"] == "withdrawn"


@pytest.mark.parametrize("operation", ["propose", "review", "publish"])
def test_manifest_change_after_writer_lock_refuses_stale_curation(case, monkeypatch, operation):
    proposal = draft(case) if operation != "propose" else None
    if operation == "publish":
        case["concepts"].decide("demo", case["catalog"], concept_id=proposal["id"], decision="approve")
    original = case["store"]._lock_source
    triggered = False
    def race(conn, tenant, cid, **kwargs):
        nonlocal triggered
        result = original(conn, tenant, cid, **kwargs)
        if not triggered:
            triggered = True
            commit_catalog(case)
        return result
    monkeypatch.setattr(case["store"], "_lock_source", race)
    with pytest.raises(KnowledgeConflict, match="manifest"):
        if operation == "propose":
            draft(case, evidence_ref=case["ref"])
        elif operation == "review":
            case["concepts"].decide("demo", case["catalog"], concept_id=proposal["id"], decision="approve")
        else:
            case["concepts"].publish("demo", case["catalog"], concept_id=proposal["id"],
                expected_proposal_digest=proposal["proposal_digest"], rationale="Race must refuse the old observation.")
    assert count(case["store"], case["store"].inventory_concept_publications) == 0


@pytest.mark.parametrize("operation", ["get", "page"])
def test_withdrawal_during_context_assembly_refuses_whole_response(case, monkeypatch, operation):
    published = publish(case)
    original = case["concepts"]._packet
    def race(*args):
        result = original(*args)
        case["concepts"].withdraw("demo", case["catalog"], concept_id=published["id"],
                                  reason="Withdraw while consumer context is assembled.")
        return result
    monkeypatch.setattr(case["concepts"], "_packet", race)
    with pytest.raises(KnowledgeConflict, match="assembly"):
        if operation == "get":
            case["concepts"].get("demo", case["catalog"], concept_id=published["id"])
        else:
            page(case)


@pytest.mark.parametrize("digest", [None, "", False, "0" * 63, "A" * 64])
def test_concept_pages_never_accept_an_unpinned_manifest(case, digest):
    with pytest.raises(KnowledgeValidationError, match="manifest digest"):
        page(case, expected_manifest_digest=digest)


def test_inventory_drafts_share_capacity_and_steward_archive_frees_slots(case, monkeypatch):
    import knowledge_layer.store as store_module

    monkeypatch.setattr(store_module, "MAX_SOURCE_PROPOSALS", 2)
    first, second = draft(case), draft(case)
    with pytest.raises(KnowledgeConflict, match="proposal limit"):
        draft(case)
    case["store"].archive_proposals("demo", case["catalog"], proposal_ids=[first["id"]],
                                   reason="Retire this draft to make space for its replacement.")
    replacement = draft(case)
    assert len({first["id"], second["id"], replacement["id"]}) == 3
    assert len(page(case, include_drafts=True)["items"]) == 2
