"""Knowledge search returns compact evidence and never upgrades candidate meaning."""

from knowledge_layer import retrieval
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.store import KnowledgeStore


def test_hybrid_search_keeps_trust_and_evidence(monkeypatch):
    class Store:
        def iter_connections(self, tenant_id):
            assert tenant_id == "demo"
            return [
                {
                    "id": "source-1",
                    "name": "Field service",
                    "snapshot": {
                        "version": 2,
                        "concepts": [
                            {
                                "id": "repair-concept",
                                "name": "Work order",
                                "semantic_type": "entity_candidate",
                                "status": "candidate",
                                "evidence_ids": ["evidence-1"],
                            }
                        ],
                        "evidence": [
                            {
                                "id": "evidence-1",
                                "summary": "Field service table name and columns were discovered.",
                                "source_pointer": "work_orders.csv",
                            }
                        ],
                    },
                }
            ]

    monkeypatch.setattr(retrieval, "_semantic_scores", lambda texts, query: [0.84])
    result = retrieval.search_knowledge(Store(), tenant_id="demo", query="repair task")
    assert result["search_mode"] == "hybrid_semantic_lexical"
    assert result["indexed_concepts"] == 1
    row = result["results"][0]
    assert row["result_kind"] == "retrieval_candidate"
    assert row["trust_state"] == "candidate"
    assert row["snapshot_version"] == 2
    assert row["evidence"][0]["source_pointer"] == "work_orders.csv"


def test_lexical_fallback_is_explicit(monkeypatch):
    class Store:
        def iter_connections(self, tenant_id):
            return [{
                "id": "source-1", "name": "Operations",
                "snapshot": {"version": 1, "concepts": [{"id": "c1", "name": "Asset", "status": "candidate", "evidence_ids": []}]},
            }]

    monkeypatch.setattr(retrieval, "_semantic_scores", lambda texts, query: None)
    result = retrieval.search_knowledge(Store(), tenant_id="demo", query="asset")
    assert result["search_mode"] == "lexical_fallback"
    assert result["results"][0]["semantic_similarity"] is None


def test_question_word_fallback_finds_current_granted_sources_without_upgrading_meaning(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "_semantic_scores", lambda _texts, _query: None)
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    try:
        identities = []
        for name in ("Identity and entity Silver", "Experiment registry gates", "Private identity experiment"):
            source = store.create_connection("demo", kind="local_files", name=name, config={})
            store.save_snapshot("demo", source["id"], {
                "status": "discovery_complete", "discovery_complete": True,
                "concepts": [{"id": name.replace(" ", "_"), "name": name,
                              "status": "candidate", "trust_state": "inferred"}],
            })
            identities.append(source["id"])
        for source_id in identities[:2]:
            store.grant_source_access("demo", source_id, principal_type="user",
                                      principal_id="reader", role="read")
        store.access_enforced = True
        with principal_scope(Principal("reader", frozenset())):
            result = retrieval.search_knowledge(
                store, tenant_id="demo", query="How do identity and experiment gates relate?", limit=10)
        assert {row["connection_id"] for row in result["results"]} == set(identities[:2])
        assert all(row["result_kind"] == "retrieval_candidate" and
                   row["trust_state"] == "inferred" for row in result["results"])
    finally:
        store.close()


def test_search_covers_concepts_after_the_old_source_order_cutoff(monkeypatch):
    class Store:
        def iter_connections(self, tenant_id):
            assert tenant_id == "demo"
            yield {"id": "recent", "name": "Recent source", "snapshot": {
                "version": 1,
                "concepts": [{"id": f"noise-{index}", "name": "Equipment maintenance"}
                             for index in range(2_500)],
            }}
            yield {"id": "older", "name": "Older source", "snapshot": {
                "version": 4,
                "concepts": [{"id": "target", "name": "Billable labor policy",
                              "status": "candidate", "evidence_ids": ["evidence-1"]}],
                "evidence": [{"id": "evidence-1", "summary": "Document heading", "source_pointer": "policy.md"}],
            }}

    sizes = []

    def scores(texts, _query):
        sizes.append(len(texts))
        return [0.0] * len(texts)

    monkeypatch.setattr(retrieval, "_semantic_scores", scores)
    result = retrieval.search_knowledge(Store(), tenant_id="demo", query="Billable labor policy")
    assert result["results"][0]["concept_id"] == "target"
    assert result["results"][0]["connection_id"] == "older"
    assert result["scanned_concepts"] == result["indexed_concepts"] == 2_501
    assert result["semantic_candidates"] == retrieval.MAX_SEMANTIC_CANDIDATES
    assert result["semantic_candidate_limit_reached"] is True
    assert result["index_limit_reached"] is False
    assert result["search_mode"] == "lexical_prefilter_semantic_rerank"
    assert sizes == [retrieval.MAX_SEMANTIC_CANDIDATES]


def test_current_index_tracks_reviewed_definition_and_refresh(monkeypatch):
    store = KnowledgeStore(database_url="sqlite:///:memory:")
    monkeypatch.setattr(retrieval, "_semantic_scores", lambda _texts, _query: None)
    try:
        source = store.create_connection("demo", kind="local_files", name="Policy", config={})
        source_id = source["id"]
        store.save_snapshot("demo", source_id, {
            "status": "discovery_complete", "discovery_complete": True,
            "assets": [{"id": "orders", "name": "Orders"}],
            "concepts": [{"id": "margin", "name": "Margin", "status": "candidate",
                          "evidence_ids": ["ev1"]}],
            "evidence": [{"id": "ev1", "summary": "Finance policy", "source_pointer": "policy.md"}],
        })
        proposal = store.submit_proposal(
            "demo", source_id, target_concept_id="margin",
            proposed_definition="Distinctive neon margin rule", evidence_ids=["ev1"],
            rationale="The finance policy defines this margin.",
        )
        store.decide_proposal("demo", source_id, proposal["id"], decision="approve")
        reviewed = retrieval.search_knowledge(
            store, tenant_id="demo", query="Distinctive neon margin rule",
        )
        assert reviewed["results"][0]["concept_id"] == "margin"
        assert reviewed["results"][0]["definition"] == "Distinctive neon margin rule"
        assert reviewed["search_mode"] == "indexed_lexical"
        store.archive_proposals(
            "demo", source_id, proposal_ids=[proposal["id"]],
            reason="Superseded by the current finance policy.",
        )
        assert retrieval.search_knowledge(
            store, tenant_id="demo", query="Distinctive neon margin rule",
        )["results"] == []
        store.save_snapshot("demo", source_id, {
            "status": "discovery_complete", "discovery_complete": True,
            "concepts": [{"id": "cost", "name": "Cost"}],
        })
        assert retrieval.search_knowledge(
            store, tenant_id="demo", query="Distinctive neon margin rule",
        )["results"] == []
        assert retrieval.search_knowledge(
            store, tenant_id="demo", query="Cost",
        )["results"][0]["concept_id"] == "cost"
    finally:
        store.close()


def test_indexed_search_drops_revoked_or_changed_source(monkeypatch):
    monkeypatch.setattr(retrieval, "_semantic_scores", lambda _texts, _query: None)

    class ChangedStore:
        current = None

        def search_concept_candidates(self, tenant_id, query, *, limit):
            return [{
                "concept_id": "private", "name": "Private ledger",
                "definition": "Restricted source policy", "semantic_type": None,
                "trust_state": "candidate", "connection_id": "source-1",
                "connection_name": "Private source", "snapshot_version": 1,
                "evidence_ids": [], "evidence": [],
                "search_text": "Private ledger Restricted source policy",
            }]

        def get_connection(self, tenant_id, connection_id):
            return self.current

    store = ChangedStore()
    assert retrieval.search_knowledge(store, tenant_id="demo", query="Private ledger")["results"] == []
    store.current = {"id": "source-1", "name": "Private source", "snapshot": {
        "version": 2, "concepts": [{"id": "private", "name": "Private ledger"}],
    }}
    assert retrieval.search_knowledge(store, tenant_id="demo", query="Private ledger")["results"] == []
