"""Source metadata is visible only to principals with an explicit grant."""

from __future__ import annotations

import json

import pytest

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.retrieval import search_knowledge
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeNotFound, KnowledgeStore


def _principal(subject: str, *, groups: tuple[str, ...] = (), manage: bool = False) -> Principal:
    return Principal(subject, frozenset(groups),
                     frozenset({"knowledge.sources.manage"}) if manage else frozenset())


def test_source_grants_filter_search_snapshot_graph_and_mutation(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", access_enforced=True)
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["owner"], "allowed_groups": [],
    }]}))
    owner = _principal("owner", manage=True)
    other = _principal("other")
    with pytest.raises(KnowledgeAccessDenied):
        store.list_connections("demo")
    with principal_scope(owner):
        source = store.create_connection("demo", kind="local_files", name="Private orders", config={})
        source_id = source["id"]
        store.save_snapshot("demo", source_id, {
            "snapshot_id": "private-v1", "status": "discovery_complete",
            "assets": [{"id": "asset_orders", "name": "Private orders"}],
            "concepts": [{"id": "concept_orders", "name": "Secret order metric"}],
            "evidence": [], "relationships": [],
        })
        assert store.count_connections("demo") == 1
        assert store.get_graph("demo", source_id)["connection_id"] == source_id
    with principal_scope(other):
        assert store.list_connections("demo") == []
        assert store.count_connections("demo") == 0
        assert store.get_connection("demo", source_id) is None
        assert store.get_snapshot("demo", source_id) is None
        assert not search_knowledge(store, tenant_id="demo", query="Secret order metric")["results"]
        with pytest.raises(KnowledgeNotFound):
            store.get_graph("demo", source_id)
        with pytest.raises(KnowledgeAccessDenied):
            store.create_connection("demo", kind="local_files", name="Other", config={})
        with pytest.raises(KnowledgeAccessDenied):
            store.refresh_connection("demo", source_id)
    store.access_enforced = False  # Offline operator operation, not a request path.
    store.grant_source_access("demo", source_id, principal_type="group", principal_id="analysts", role="read")
    store.access_enforced = True
    analyst = _principal("analyst", groups=("analysts",))
    with principal_scope(analyst):
        assert store.count_connections("demo") == 1
        assert store.get_snapshot("demo", source_id)["id"] == "private-v1"
        assert store.get_graph("demo", source_id)["connection_id"] == source_id
        with pytest.raises(KnowledgeAccessDenied):
            store.refresh_connection("demo", source_id)
    store.access_enforced = False
    store.revoke_source_access("demo", source_id, principal_type="group", principal_id="analysts")
    store.access_enforced = True
    with principal_scope(analyst):
        assert store.get_snapshot("demo", source_id) is None
    store.close()
