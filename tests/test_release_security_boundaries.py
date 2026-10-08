"""Regression tests for bounded background work and post-revocation writes."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from knowledge_layer import retrieval
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeNotFound, KnowledgeStore


def test_cold_embedding_backlog_keeps_every_unfinished_future_in_a_bounded_slot(monkeypatch):
    blocked = Event()
    started = Event()
    executed = []

    def build(texts):
        executed.append(texts)
        started.set()
        assert blocked.wait(timeout=5)
        return texts

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = OrderedDict()
        monkeypatch.setattr(retrieval, "_BUILD_POOL", pool)
        monkeypatch.setattr(retrieval, "_VECTOR_FUTURES", futures)
        monkeypatch.setattr(retrieval, "_vectors_cached", build)
        try:
            assert retrieval._vectors(("first",)) is None
            assert started.wait(timeout=2)
            # Every different query corpus used to enqueue work after evicting
            # the dictionary reference. All unfinished work must stay bounded.
            with ThreadPoolExecutor(max_workers=8) as callers:
                results = list(callers.map(lambda n: retrieval._vectors((str(n),)), range(200)))
            assert all(result is None for result in results)
            assert len(futures) == 8
            assert ("first",) in futures
            assert len(executed) == 1
        finally:
            blocked.set()
        for future in list(futures.values()):
            future.result(timeout=2)
        assert len(executed) == 8
        # Completed slots can be replaced without losing unfinished work.
        retrieval._vectors(("after_completion",))
        futures[("after_completion",)].result(timeout=2)
        assert retrieval._vectors(("after_completion",)) == ("after_completion",)
        assert len(futures) == 8
        assert len(executed) == 9


@pytest.mark.parametrize("mutation", ["configure", "semantic_proposal", "contract_proposal"])
def test_write_rechecks_source_grant_after_preflight_before_mutation(tmp_path, monkeypatch, mutation):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    source_id = source["id"]
    snapshot = store.save_snapshot("demo", source_id, {
        "snapshot_id": "orders-v1", "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "orders", "name": "Orders", "asset_type": "table",
                    "schema": [{"name": "order_id", "data_type": "text"}]}],
        "concepts": [{"id": "concept_orders", "name": "Orders"}],
        "relationships": [],
        "evidence": [{"id": "orders-schema", "asset_id": "orders", "summary": "Orders schema",
                      "evidence_type": "asset_metadata"}],
    })
    store.grant_source_access("demo", source_id, principal_type="user", principal_id="owner", role="manage")
    store.access_enforced = True
    store.source_policy = SourcePolicy.from_json('{"version":1,"sources":[{"tenant_id":"demo",'
        '"kind":"local_files","selector":{"path":".","include":"**/*"},'
        '"allowed_subjects":["owner"],"allowed_groups":[]}]}')
    principal = Principal("owner", frozenset(), frozenset({"knowledge.sources.manage"}))
    original_config = source["config"]
    revoked = False

    def revoke():
        nonlocal revoked
        store.access_enforced = False
        store.revoke_source_access("demo", source_id, principal_type="user", principal_id="owner")
        store.access_enforced = True
        revoked = True

    if mutation == "configure":
        original = store._require_source_approval

        def preflight(*args, **kwargs):
            original(*args, **kwargs)
            revoke()

        monkeypatch.setattr(store, "_require_source_approval", preflight)
        action = lambda: store.update_connection_config("demo", source_id, {**original_config, "profile_rows": 1})
    else:
        original = store._require_proposal_capacity

        def capacity(*args, **kwargs):
            revoke()
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "_require_proposal_capacity", capacity)
        if mutation == "semantic_proposal":
            action = lambda: store.submit_proposal("demo", source_id, target_concept_id="concept_orders",
                proposed_definition="Orders in this source.", evidence_ids=["orders-schema"],
                rationale="The source schema supports this definition.")
        else:
            action = lambda: store.submit_contract_assertion("demo", source_id,
                assertion_kind="grain", asset_id="orders",
                payload={"description": "One row per order", "key_fields": ["order_id"]},
                evidence_ids=["orders-schema"], rationale="The source key identifies the order.",
                expected_snapshot_id=snapshot["id"])
    try:
        with principal_scope(principal), pytest.raises(KnowledgeNotFound):
            action()
        assert revoked
        store.access_enforced = False
        assert store.get_connection("demo", source_id)["config"] == original_config
        assert store._list_local_proposals("demo", source_id) == []
        assert store._list_contract_proposals("demo", source_id) == []
    finally:
        store.close()
