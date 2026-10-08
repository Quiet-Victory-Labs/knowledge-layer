"""Request-local expansion reduces work without retaining stale authority."""

from copy import deepcopy

import pytest
from sqlalchemy import event, select

from knowledge_layer import retrieval
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.store import KnowledgeConflict, KnowledgeStore, KnowledgeValidationError


def _snapshot(*, label="Order", count=15):
    return {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "orders", "name": "Orders", "asset_type": "table",
                    "source_pointer": "orders.csv", "schema": [{"name": "value"}]}],
        "concepts": [{"id": f"concept-{index:02}", "name": f"{label} field {index}",
                      "semantic_type": "field_candidate", "status": "candidate",
                      "source_binding": {"asset_id": "orders", "source_pointer": "orders.csv"},
                      "evidence_ids": ["ev1"]} for index in range(count)],
        "evidence": [{"id": "ev1", "summary": "Observed source schema",
                      "source_pointer": "orders.csv", "evidence_type": "schema"}],
        "relationships": [{"id": "rel1", "subject_id": "concept-00", "predicate": "related_to",
                           "object_id": "concept-01", "evidence_ids": ["ev1"]}],
    }


@pytest.fixture
def registry(tmp_path):
    url = f"sqlite:///{tmp_path / 'registry.db'}"
    operator = KnowledgeStore(database_url=url)
    sources = []
    for name in ("Orders", "Other orders"):
        source = operator.create_connection("demo", kind="local_files", name=name, config={})
        operator.save_snapshot("demo", source["id"], _snapshot())
        operator.grant_source_access("demo", source["id"], principal_type="user",
                                     principal_id="reader", role="read")
        sources.append(source["id"])
    guarded = KnowledgeStore(database_url=url, initialize_schema=False, access_enforced=True)
    yield operator, guarded, sources
    guarded.close()
    operator.close()


def test_scoped_context_preserves_metadata_with_bounded_queries(registry, monkeypatch):
    _, store, sources = registry
    with principal_scope(Principal("reader", frozenset())):
        concepts = store.get_concepts("demo")
        selected = sorted([item for item in concepts if item["connection_id"] == sources[0]],
                          key=lambda item: item["id"])[:10]
        expected = [store._context_for_concept("demo", item, concepts) for item in selected]
        projections, queries = [], []
        original = store._project_snapshot_row

        def project(*args, **kwargs):
            projections.append(args[1])
            return original(*args, **kwargs)

        def query(_conn, _cursor, statement, _parameters, _context, _executemany):
            queries.append(statement)

        def unused_contract(*_args, **_kwargs):
            pytest.fail("A source without approved assertions cannot return a contract citation or metric")

        monkeypatch.setattr(store, "_project_snapshot_row", project)
        monkeypatch.setattr("knowledge_layer.contracts.semantic_contract", unused_contract)
        event.listen(store.engine, "before_cursor_execute", query)
        try:
            actual = store.get_context("demo", term="Order", connection_id=sources[0])
        finally:
            event.remove(store.engine, "before_cursor_execute", query)
        assert actual["matches"] == expected
        assert actual["total_matches"] == 15 and actual["more_available"] is True
        assert actual["retrieval_candidates"] == actual["metric_matches"] == []
        # One scoped projection plus two fresh final projections. Shared-label
        # discovery reads only metadata and does not project either source.
        assert len(projections) == 3
        assert len(queries) <= 25


@pytest.mark.parametrize("change", ["revoke", "refresh", "review", "cross_revoke", "cross_type"])
def test_scoped_context_refuses_changed_authority_before_return(registry, monkeypatch, change):
    operator, store, sources = registry
    original = store._context_connections
    changed = False

    def fresh(tenant, ids):
        nonlocal changed
        if not changed:
            changed = True
            if change in {"revoke", "cross_revoke"}:
                operator.revoke_source_access("demo", sources[change == "cross_revoke"],
                                              principal_type="user", principal_id="reader")
            elif change == "refresh":
                operator.save_snapshot("demo", sources[0], _snapshot(label="Replacement"))
            elif change == "cross_type":
                snapshot = _snapshot()
                snapshot["concepts"][0]["semantic_type"] = "different_type"
                operator.save_snapshot("demo", sources[1], snapshot)
            else:
                proposal = operator.submit_proposal(
                    "demo", sources[0], target_concept_id="concept-00",
                    proposed_definition="Reviewed order field meaning", evidence_ids=["ev1"],
                    rationale="Review the source-backed field meaning.",
                )
                operator.decide_proposal("demo", sources[0], proposal["id"], decision="approve")
        return original(tenant, ids)

    monkeypatch.setattr(store, "_context_connections", fresh)
    with principal_scope(Principal("reader", frozenset())), pytest.raises(KnowledgeConflict, match="context changed"):
        store.get_context("demo", term="Order", connection_id=sources[0])
    assert changed


def test_context_has_no_cache_between_requests(registry):
    operator, store, sources = registry
    with principal_scope(Principal("reader", frozenset())):
        assert store.get_context("demo", term="Order", connection_id=sources[0])["matches"]
    operator.save_snapshot("demo", sources[0], _snapshot(label="Replacement"))
    with principal_scope(Principal("reader", frozenset())):
        assert store.get_context("demo", term="Order", connection_id=sources[0])["matches"] == []
        assert store.get_context("demo", term="Replacement", connection_id=sources[0])["matches"]
    operator.revoke_source_access("demo", sources[0], principal_type="user", principal_id="reader")
    with principal_scope(Principal("reader", frozenset())):
        assert store.get_context("demo", term="Replacement", connection_id=sources[0]) is None


def test_indexed_search_batches_fresh_sources_and_preserves_records(registry, monkeypatch):
    _, store, sources = registry
    monkeypatch.setattr(retrieval, "_semantic_scores", lambda _texts, _query: None)
    batches = []
    original = store._context_connections

    def fresh(tenant, ids):
        batches.append(ids)
        return original(tenant, ids)

    monkeypatch.setattr(store, "_context_connections", fresh)
    with principal_scope(Principal("reader", frozenset())):
        result = retrieval.search_knowledge(store, tenant_id="demo", query="Order", limit=10)
        assert len(batches) == 1 and set(batches[0]) == set(sources)
        current = {source_id: {row["concept_id"]: row for row in retrieval._connection_records(
            store.get_connection("demo", source_id))} for source_id in sources}
        assert len(result["results"]) == 10
        for row in result["results"]:
            record = current[row["connection_id"]][row["concept_id"]]
            assert {key: row[key] for key in record if key != "text"} == {
                key: value for key, value in record.items() if key != "text"}


@pytest.mark.parametrize("change", ["revoke", "refresh"])
def test_search_batch_after_ranking_drops_changed_source(registry, monkeypatch, change):
    operator, store, sources = registry

    def score(_texts, _query):
        if change == "revoke":
            operator.revoke_source_access("demo", sources[0], principal_type="user", principal_id="reader")
        else:
            operator.save_snapshot("demo", sources[0], _snapshot(label="Replacement"))

    monkeypatch.setattr(retrieval, "_semantic_scores", score)
    with principal_scope(Principal("reader", frozenset())):
        result = retrieval.search_knowledge(store, tenant_id="demo", query="Order", limit=10)
        assert all(row["connection_id"] != sources[0] for row in result["results"])


def test_context_batch_caps_source_count(registry):
    _, store, _ = registry
    with principal_scope(Principal("reader", frozenset())), pytest.raises(KnowledgeValidationError):
        store._context_connections("demo", [f"source-{index}" for index in range(21)])


@pytest.mark.parametrize("change", ["revoke", "refresh", "review", "cross_revoke", "cross_type"])
def test_direct_context_refuses_changes_at_final_read(registry, monkeypatch, change):
    operator, store, sources = registry
    original = store._context_connections

    def fresh(tenant, ids):
        if change in {"revoke", "cross_revoke"}:
            operator.revoke_source_access("demo", sources[change == "cross_revoke"],
                                          principal_type="user", principal_id="reader")
        elif change == "refresh":
            operator.save_snapshot("demo", sources[0], _snapshot(label="Replacement"))
        elif change == "cross_type":
            snapshot = _snapshot()
            snapshot["concepts"][0]["semantic_type"] = "different_type"
            operator.save_snapshot("demo", sources[1], snapshot)
        else:
            proposal = operator.submit_proposal(
                "demo", sources[0], target_concept_id="concept-00",
                proposed_definition="Reviewed order field meaning", evidence_ids=["ev1"],
                rationale="Review the source-backed field meaning.",
            )
            operator.decide_proposal("demo", sources[0], proposal["id"], decision="approve")
        return original(tenant, ids)

    monkeypatch.setattr(store, "_context_connections", fresh)
    with principal_scope(Principal("reader", frozenset())), pytest.raises(KnowledgeConflict, match="context changed"):
        store.get_context("demo", concept_id="concept-00", connection_id=sources[0])


@pytest.mark.parametrize("change", ["revoke", "refresh", "review"])
def test_direct_context_refuses_change_after_concept_lookup(registry, monkeypatch, change):
    operator, store, _ = registry
    original = store.get_concept

    def concept(tenant, identifier):
        result = original(tenant, identifier)
        if change == "revoke":
            operator.revoke_source_access("demo", result["connection_id"], principal_type="user", principal_id="reader")
        elif change == "refresh":
            operator.save_snapshot("demo", result["connection_id"], _snapshot(label="Replacement"))
        else:
            proposal = operator.submit_proposal(
                "demo", result["connection_id"], target_concept_id="concept-00",
                proposed_definition="Reviewed order field meaning", evidence_ids=["ev1"],
                rationale="Review the source-backed field meaning.",
            )
            operator.decide_proposal("demo", result["connection_id"], proposal["id"], decision="approve")
        return result

    monkeypatch.setattr(store, "get_concept", concept)
    with principal_scope(Principal("reader", frozenset())), pytest.raises(KnowledgeConflict, match="context changed"):
        store.get_context("demo", concept_id="concept-00")


def test_context_expansion_requires_its_current_source_and_concept(registry):
    operator, store, sources = registry
    with principal_scope(Principal("reader", frozenset())):
        concept = store.get_concepts("demo", sources[0])[0]
        other = store.get_connection("demo", sources[1])
        with pytest.raises(KnowledgeConflict, match="context changed"):
            store._context_for_concept("demo", concept, [], other)
    operator.save_snapshot("demo", sources[0], _snapshot(label="Replacement"))
    with principal_scope(Principal("reader", frozenset())), pytest.raises(KnowledgeConflict, match="context changed"):
        store._context_for_concept("demo", concept, [])
    operator.revoke_source_access("demo", sources[0], principal_type="user", principal_id="reader")
    with principal_scope(Principal("reader", frozenset())), pytest.raises(KnowledgeConflict, match="context changed"):
        store._context_for_concept("demo", concept, [])


def test_direct_context_matches_term_metadata_and_checks_next_request(registry):
    operator, store, sources = registry
    with principal_scope(Principal("reader", frozenset())):
        term = store.get_context("demo", term="Order field 0", connection_id=sources[0])
        assert store.get_context("demo", concept_id="concept-00", connection_id=sources[0]) == term["matches"][0]
    operator.revoke_source_access("demo", sources[0], principal_type="user", principal_id="reader")
    with principal_scope(Principal("reader", frozenset())):
        assert store.get_context("demo", concept_id="concept-00", connection_id=sources[0]) is None


def test_context_label_hints_preserve_current_order_labels_types_and_grants(registry):
    operator, store, sources = registry
    snapshot = _snapshot()
    snapshot["concepts"][0]["name"] = "  Order_Field 0  "
    snapshot["concepts"][1]["name"] = "Order field 0"
    snapshot["concepts"][1]["semantic_type"] = "different_type"
    snapshot["concepts"][2]["name"] = "Order field 0"
    operator.save_snapshot("demo", sources[1], snapshot)
    proposal = operator.submit_proposal(
        "demo", sources[1], target_concept_id="concept-00",
        proposed_definition="Reviewed order field meaning", evidence_ids=["ev1"],
        rationale="Review the source-backed field meaning.",
    )
    operator.decide_proposal("demo", sources[1], proposal["id"], decision="approve")
    hidden = operator.create_connection("demo", kind="local_files", name="Hidden", config={})
    operator.save_snapshot("demo", hidden["id"], _snapshot())
    with principal_scope(Principal("reader", frozenset())):
        current = store.get_concepts("demo")
        selected = next(item for item in current if item["connection_id"] == sources[0]
                        and item["id"] == "concept-00")
        expected = [
            {key: item.get(key) for key in
             ("id", "name", "semantic_type", "connection_id", "connection_name")}
            for item in current
            if retrieval.normalize_name(str(item.get("name") or "")) == "order field 0"
            and item.get("semantic_type") == selected["semantic_type"]
        ]
        assert store._context_cross_source_concepts("demo", [selected]) == expected
        assert all(item["connection_id"] != hidden["id"] for item in expected)
        context = store.get_context("demo", term="Order field 0", connection_id=sources[0])
        assert context["matches"][0]["cross_source_candidates"] == store._context_for_concept(
            "demo", selected, current)["cross_source_candidates"]


def test_snapshot_projection_does_not_mutate_payload_or_local_proposal_inputs(registry):
    operator, _, sources = registry
    proposal = operator.submit_proposal(
        "demo", sources[0], target_concept_id="concept-00",
        proposed_definition="Reviewed order field meaning", evidence_ids=["ev1"],
        rationale="Review the source-backed field meaning.",
    )
    operator.decide_proposal("demo", sources[0], proposal["id"], decision="approve")
    with operator.engine.connect() as connection:
        row = dict(connection.execute(select(operator.snapshots).where(
            operator.snapshots.c.tenant_id == "demo",
            operator.snapshots.c.connection_id == sources[0],
        ).order_by(operator.snapshots.c.version.desc()).limit(1)).mappings().one())
    proposals = operator._list_local_proposals("demo", sources[0])
    before_row, before_proposals = deepcopy(row), deepcopy(proposals)
    projected = operator._project_snapshot_row(
        "demo", sources[0], row, proposals, {item["id"] for item in proposals},
    )
    assert projected["concepts"][0]["definition_status"] == "approved"
    projected["concepts"][0]["definition"] = "Caller modification"
    projected["assets"][0]["schema"][0]["name"] = "Caller modification"
    projected["proposals"][0]["evidence_ids"].append("Caller modification")
    assert row == before_row
    assert proposals == before_proposals
