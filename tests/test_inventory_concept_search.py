"""Effects of bounded published-inventory retrieval; registry metadata only."""

import json
from copy import deepcopy

import pytest
from sqlalchemy import delete, event, insert, update
from test_inventory_citations import commit_catalog, setup_case
from test_inventory_concepts import configure_access, draft, publish

from knowledge_layer import retrieval
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory_concept_search import InventoryConceptSearch, resolve_inventory_term
from knowledge_layer.inventory_concepts import InventoryConcepts, _digest
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeConflict, KnowledgeNotFound, KnowledgeValidationError


@pytest.fixture
def case(tmp_path, monkeypatch):
    fixture = setup_case(tmp_path)
    fixture["concepts"] = InventoryConcepts(fixture["store"])
    monkeypatch.setattr(retrieval, "_semantic_scores", lambda texts, query: None)
    try:
        yield fixture
    finally:
        fixture["store"].close()


def test_global_and_term_consumption_preserve_complete_inventory_provenance_without_snapshot(case, monkeypatch):
    definition = "Finance interprets net revenue after refunded amounts, in USD. " * 30 + "The scope remains exact."
    packet = publish(case, draft(case, definition=definition))
    store, cid = case["store"], case["catalog"]
    assert store.get_snapshot("demo", cid) is None
    monkeypatch.setattr(store, "get_concepts", lambda *a, **k: pytest.fail("inventory term scanned legacy concepts"))
    monkeypatch.setattr(store, "iter_connections", lambda *a, **k: pytest.fail("inventory search scanned source registry"))
    found = retrieval.search_knowledge(store, tenant_id="demo", query="Net revenue")
    row = found["results"][0]
    assert row["concept_id"] == packet["id"] and row["basis"] == "inventory"
    assert row["definition"] == definition and row["context"] == packet
    assert row["evidence_ref"] == case["ref"] and set(row["evidence_ref"]) == {
        "connection_id", "inventory_id", "manifest_digest", "entity_id", "evidence_id"}
    assert row["publication_state"] == "published" and row["trust_state"] == "locally_reviewed"
    assert not row["native_meaning_certified"] and not row["locally_executable"]
    assert "snapshot_id" not in row and "native_record" not in json.dumps(found)
    context = store.get_context("demo", term="Net revenue", connection_id=cid)
    assert context["matches"] == [packet]
    assert context["metric_matches"] == [] and not context["retrieval_candidates"]
    assert context["inventory_search_complete"] and context["inventory_candidates_checked"] == 1
    assert context["inventory_candidate_digest"] == InventoryConceptSearch(store).candidates(
        "demo", "Net revenue", cid)["candidate_digest"]


@pytest.mark.parametrize("term,basis", [("refunded amounts", "text_phrase"), ("revenues net", "token_overlap")])
def test_definition_phrases_and_reordered_plural_labels_are_suggestions_not_aliases(case, term, basis):
    packet = publish(case)
    result = resolve_inventory_term(case["store"], "demo", term, case["catalog"])
    assert result["matches"] == []
    candidate = result["retrieval_candidates"][0]
    assert candidate["candidate_type"] == "inventory_concept"
    assert candidate["requires_confirmation"] and candidate["match_basis"] == basis
    assert candidate["context"] == packet


def test_same_label_conflicts_are_separate_and_connection_scope_remains_exact(case):
    one = publish(case)
    other = case["store"].create_connection("demo", kind="local_files", name="Another catalog", config={"path": "another_catalog"})
    second_case = {**case, "catalog": other["id"]}
    commit_catalog(second_case)
    two = publish(second_case, draft(second_case, definition="Sales uses net revenue to mean its separate invoiced measure."))
    found = resolve_inventory_term(case["store"], "demo", "Net revenue")
    assert {item["id"] for item in found["matches"]} == {one["id"], two["id"]}
    assert {item["concept"]["definition"] for item in found["matches"]} == {
        one["concept"]["definition"], two["concept"]["definition"]}
    assert resolve_inventory_term(case["store"], "demo", "Net revenue", case["catalog"])["matches"] == [one]
    assert resolve_inventory_term(case["store"], "demo", "Net revenue", case["target"])["matches"] == []


@pytest.mark.parametrize("state", ["draft", "approved", "withdrawn", "archived", "review_digest", "publication_digest"])
def test_only_matching_review_and_current_publication_enter_search(case, state):
    proposal = draft(case)
    store = case["store"]
    if state != "draft":
        store.decide_proposal("demo", case["catalog"], proposal["id"], decision="approve")
    if state not in {"draft", "approved"}:
        publish(case, proposal)
        if state == "withdrawn":
            case["concepts"].withdraw("demo", case["catalog"], concept_id=proposal["id"], reason="Local owner withdrew meaning.")
        else:
            table, values = {
                "archived": (store.inventory_concept_proposals, {"archived_at": "2026-10-04T00:00:00Z"}),
                "review_digest": (store.decisions, {"proposal_digest": "0" * 64}),
                "publication_digest": (store.inventory_concept_publications, {"proposal_digest": "0" * 64}),
            }[state]
            with store.engine.begin() as conn:
                conn.execute(update(table).values(**values))
    metadata = InventoryConceptSearch(store).candidates("demo", "Net revenue")
    assert metadata["items"] == []
    assert resolve_inventory_term(store, "demo", "Net revenue")["total_matches"] == 0
    assert retrieval.search_knowledge(store, tenant_id="demo", query="Net revenue")["results"] == []


@pytest.mark.parametrize("change", ["successor", "empty", "error", "identity", "active_refresh", "profile"])
def test_stale_source_head_profile_and_refresh_are_excluded_before_counts(case, monkeypatch, change):
    publish(case)
    store = case["store"]
    if change in {"successor", "empty"}:
        commit_catalog(case, empty=change == "empty")
    elif change == "error":
        with store.engine.begin() as conn:
            conn.execute(update(store.connections).where(store.connections.c.id == case["catalog"]).values(status="error"))
    elif change == "identity":
        store.update_connection_config("demo", case["catalog"], {"path": "changed_catalog"})
    elif change == "active_refresh":
        RefreshQueue(store).enqueue("demo", case["catalog"])
    else:
        from knowledge_layer.inventory import InventoryStore

        def changed_profile(*args, **kwargs):
            raise KnowledgeConflict("Inventory adapter identity or revision changed")
        monkeypatch.setattr(InventoryStore, "_current_committed", changed_profile)
    result = resolve_inventory_term(store, "demo", "Net revenue")
    assert result["matches"] == [] and result["total_matches"] == result["inventory_candidates_checked"] == 0
    assert retrieval.search_knowledge(store, tenant_id="demo", query="Net revenue")["results"] == []


def test_grants_and_operator_approval_filter_names_definitions_and_counts(case):
    owner, _, reader = configure_access(case)
    with principal_scope(owner):
        publish(case)
    with principal_scope(reader):
        assert resolve_inventory_term(case["store"], "demo", "Net revenue")["total_matches"] == 1
    with principal_scope(Principal("stranger", frozenset())):
        result = resolve_inventory_term(case["store"], "demo", "Net revenue")
        assert result["matches"] == [] and result["total_matches"] == 0
        assert "Finance uses" not in json.dumps(result)
    case["store"].source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": []}))
    with principal_scope(reader):
        result = resolve_inventory_term(case["store"], "demo", "Net revenue")
        assert result["inventory_candidates_checked"] == 0 and result["matches"] == []


def test_integrity_tampering_never_returns_unreviewed_definition(case):
    packet = publish(case)
    with case["store"].engine.begin() as conn:
        conn.execute(update(case["store"].inventory_concept_proposals).where(
            case["store"].inventory_concept_proposals.c.id == packet["id"]).values(definition="Unreviewed injected definition"))
    result = resolve_inventory_term(case["store"], "demo", "Net revenue")
    assert result["matches"] == [] and result["inventory_candidates_checked"] == 0


@pytest.mark.parametrize("record,change", [("entity", "digest"), ("evidence", "digest"), ("entity", "missing"), ("evidence", "missing")])
def test_exact_registry_record_digests_are_checked_before_returned_counts(case, record, change):
    publish(case)
    table = case["store"].inventory_entities if record == "entity" else case["store"].inventory_evidence
    with case["store"].engine.begin() as conn:
        if change == "missing":
            conn.execute(delete(table))
        else:
            conn.execute(update(table).values(digest="0" * 64))
    result = resolve_inventory_term(case["store"], "demo", "Net revenue")
    assert result["matches"] == [] and result["inventory_candidates_checked"] == result["total_matches"] == 0


def test_reader_grant_revoked_during_selected_response_assembly_refuses_all_content(case, monkeypatch):
    owner, _, reader = configure_access(case)
    with principal_scope(owner):
        publish(case)
    original = InventoryConceptSearch.resolve
    def revoke(self, tenant, row):
        packet = original(self, tenant, row)
        store = case["store"]
        store.access_enforced = False
        try:
            store.revoke_source_access("demo", case["catalog"], principal_type="user", principal_id="reader")
        finally:
            store.access_enforced = True
        return packet
    monkeypatch.setattr(InventoryConceptSearch, "resolve", revoke)
    with principal_scope(reader), pytest.raises(KnowledgeNotFound):
        resolve_inventory_term(case["store"], "demo", "Net revenue")


@pytest.mark.parametrize("surface", ["global", "term"])
def test_earlier_selected_packet_is_rechecked_after_later_packet_assembly(case, monkeypatch, surface):
    first = publish(case)
    second = publish(case)
    original = InventoryConceptSearch.resolve
    calls = 0
    def withdraw_earlier(self, tenant, row):
        nonlocal calls
        packet = original(self, tenant, row)
        calls += 1
        if calls == 2:
            earlier = first if row["id"] == second["id"] else second
            case["concepts"].withdraw("demo", case["catalog"], concept_id=earlier["id"], reason="Withdrawn during response assembly.")
        return packet
    monkeypatch.setattr(InventoryConceptSearch, "resolve", withdraw_earlier)
    with pytest.raises((KnowledgeConflict, KnowledgeNotFound)) as error:
        if surface == "global":
            retrieval.search_knowledge(case["store"], tenant_id="demo", query="Net revenue")
        else:
            resolve_inventory_term(case["store"], "demo", "Net revenue")
    assert "published inventory concept" in str(error.value) or "changed" in str(error.value)


def populate_published_metadata(case, count, *, definition=None, names=None):
    """Clone real reviewed packets with valid digests; evidence stays exact."""
    seed = publish(case)
    store = case["store"]
    with store.engine.begin() as conn:
        from sqlalchemy import select

        proposal = dict(conn.execute(select(store.inventory_concept_proposals)).mappings().one())
        decision = dict(conn.execute(select(store.decisions).where(store.decisions.c.proposal_id == seed["id"])).mappings().one())
        publication = dict(conn.execute(select(store.inventory_concept_publications)).mappings().one())
        for number in range(1, count):
            row = {**deepcopy(proposal), "id": f"inventory_concept_{number:032x}"}
            if names is not None:
                row["name"] = names(number)
            if definition is not None:
                row["definition"] = definition
            row["proposal_digest"] = _digest(row)
            conn.execute(insert(store.inventory_concept_proposals).values(**row))
            conn.execute(insert(store.decisions).values(**{**decision, "proposal_id": row["id"], "proposal_digest": row["proposal_digest"]}))
            conn.execute(insert(store.inventory_concept_publications).values(**{**publication, "concept_id": row["id"], "proposal_digest": row["proposal_digest"]}))


def test_candidate_and_exact_selection_bounds_are_explicit_and_pool_digest_is_stable(case, monkeypatch):
    populate_published_metadata(case, 257)
    sql = []
    event.listen(case["store"].engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: sql.append(statement))
    search = InventoryConceptSearch(case["store"])
    one = search.candidates("demo", "Net revenue")
    assert len(one["items"]) == 256 and one["limit_reached"]
    assert one["candidate_digest"] == search.candidates("demo", "Net revenue")["candidate_digest"]
    assert any("knowledge_inventory_heads" in statement and "knowledge_refresh_jobs" in statement
               and "knowledge_proposal_decisions" in statement for statement in sql)
    selected = []
    original = InventoryConceptSearch.resolve
    def track(self, tenant, row):
        selected.append(row["id"])
        return original(self, tenant, row)
    monkeypatch.setattr(InventoryConceptSearch, "resolve", track)
    result = resolve_inventory_term(case["store"], "demo", "Net revenue")
    assert len(result["matches"]) == len(selected) == 10
    assert result["inventory_candidate_limit_reached"] and not result["inventory_search_complete"]
    assert result["total_matches"] == 256 and result["counts_basis"].endswith("bounded_metadata_candidates")
    selected.clear()
    global_result = retrieval.search_knowledge(case["store"], tenant_id="demo", query="Net revenue", limit=10)
    assert len(global_result["results"]) == len(selected) == 10
    assert global_result["semantic_candidates"] == 256 and global_result["index_limit_reached"]


def test_new_matching_publication_changes_authenticated_pool_pin(case):
    publish(case)
    before = resolve_inventory_term(case["store"], "demo", "Net revenue")
    publish(case)
    after = InventoryConceptSearch(case["store"]).candidates("demo", "Net revenue")
    assert before["inventory_candidate_digest"] != after["candidate_digest"]


def test_term_resolves_at_most_ten_direct_plus_ten_suggestions_and_rechecks_once(case, monkeypatch):
    populate_published_metadata(case, 24, names=lambda number: "Net revenue" if number < 12 else "Finance glossary")
    seen, checks = [], []
    original = InventoryConceptSearch.resolve
    final = InventoryConceptSearch.recheck
    def track(self, tenant, row):
        seen.append(row["id"])
        return original(self, tenant, row)
    def track_final(self, tenant, packets):
        checks.append(len(packets))
        return final(self, tenant, packets)
    monkeypatch.setattr(InventoryConceptSearch, "resolve", track)
    monkeypatch.setattr(InventoryConceptSearch, "recheck", track_final)
    result = case["store"].get_context("demo", term="Net revenue", connection_id=case["catalog"])
    assert len(result["matches"]) == len(result["retrieval_candidates"]) == 10
    assert len(seen) == 20 and checks == [20]
    assert result["more_available"] and result["retrieval_candidate_more_available"]
    assert all(item["requires_confirmation"] for item in result["retrieval_candidates"])


def test_plain_legacy_source_keeps_term_envelope_and_inventory_zero_match_is_pinned(case):
    store = case["store"]
    legacy = store.get_context("demo", term="unknown", connection_id=case["target"])
    assert not any(key.startswith("inventory_") for key in legacy)
    assert "counts_basis" not in legacy
    inventory = store.get_context("demo", term="unknown", connection_id=case["catalog"])
    assert inventory["inventory_present"] and inventory["matches"] == []
    assert len(inventory["inventory_candidate_digest"]) == 64
    commit_catalog(case, empty=True)
    empty = store.get_context("demo", term="unknown", connection_id=case["catalog"])
    assert empty["inventory_present"] and empty["inventory_candidates_checked"] == 0
    assert empty["inventory_search_complete"]


def test_snapshot_source_can_also_retrieve_current_inventory_overlay(case):
    overlay = {**case, "catalog": case["target"]}
    commit_catalog(overlay)
    packet = publish(overlay)
    result = case["store"].get_context("demo", term="Net revenue", connection_id=case["target"])
    assert result["matches"] == [packet]
    assert case["store"].get_snapshot("demo", case["target"]) is not None


def test_sqlite_work_budget_refuses_and_restores_connection_after_interruption(case, monkeypatch):
    populate_published_metadata(case, 64)
    import knowledge_layer.inventory_concept_search as module

    ticks = iter(range(0, 100_000, 10))
    with monkeypatch.context() as scoped:
        scoped.setattr(module, "monotonic", lambda: next(ticks))
        with pytest.raises(KnowledgeConflict, match="SQL budget exceeded"):
            InventoryConceptSearch(case["store"]).candidates("demo", "Net revenue")
    assert len(InventoryConceptSearch(case["store"]).candidates("demo", "Net revenue")["items"]) == 64


def test_response_budget_refuses_complete_packet_instead_of_clipping(case, monkeypatch):
    publish(case)
    import knowledge_layer.inventory_concept_search as module

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(KnowledgeValidationError, match="256 KiB"):
        resolve_inventory_term(case["store"], "demo", "Net revenue")
    with pytest.raises(KnowledgeValidationError, match="256 KiB"):
        retrieval.search_knowledge(case["store"], tenant_id="demo", query="Net revenue")
