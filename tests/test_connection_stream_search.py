"""Cross-source evidence search must reach older granted sources without leaking others."""

from __future__ import annotations

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.contracts import search_document_evidence
from knowledge_layer.store import KnowledgeStore


def test_document_search_reaches_older_source_outside_list_page_and_respects_grants(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    oldest = store.create_connection("demo", kind="local_files", name="Policy archive", config={})
    store.save_snapshot("demo", oldest["id"], {
        "status": "discovery_complete", "discovery_complete": True,
        "evidence": [
            {"id": "billable-policy", "evidence_type": "document_derived_term",
             "summary": "Billable labor policy", "source_pointer": "policy.md"},
            {"id": "billable-travel", "evidence_type": "document_derived_term",
             "summary": "Billable travel", "source_pointer": "travel.md"},
            {"id": "labor-definition", "evidence_type": "document_derived_term",
             "summary": "Labor definition", "source_pointer": "labor.md"},
        ],
    })
    noise = [store.create_connection("demo", kind="local_files", name=f"Source {index:03d}", config={})
             for index in range(128)]
    hidden = store.create_connection("demo", kind="local_files", name="Private policy", config={})
    store.save_snapshot("demo", hidden["id"], {
        "status": "discovery_complete", "discovery_complete": True,
        "evidence": [{"id": "private-evidence", "evidence_type": "document_derived_term",
                      "summary": "Billable labor private exception", "source_pointer": "private.md"}],
    })
    assert oldest["id"] not in {row["id"] for row in store.list_connections("demo", limit=30)}
    for source in [oldest, *noise]:
        store.grant_source_access("demo", source["id"], principal_type="user",
                                  principal_id="reader", role="read")
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset())):
        assert len(list(store.iter_connections("demo"))) == 129
        result = search_document_evidence(store, tenant_id="demo", query="billable labor", limit=2)
        assert result["total_matches"] == 1
        assert [row["evidence_ref"]["evidence_id"] for row in result["matches"]] == ["billable-policy"]
        for query, expected in (("billable", {"billable-policy", "billable-travel"}),
                                ("labor", {"billable-policy", "labor-definition"})):
            single_term = search_document_evidence(store, tenant_id="demo", query=query, limit=2)
            assert single_term["total_matches"] == 2
            assert {row["evidence_ref"]["evidence_id"] for row in single_term["matches"]} == expected
            for row in [*result["matches"], *single_term["matches"]]:
                assert row["connection_id"] == oldest["id"]
                assert row["evidence_ref"]["snapshot_id"]
    store.access_enforced = False
    store.save_snapshot("demo", oldest["id"], {
        "status": "discovery_complete", "discovery_complete": True,
        "evidence": [{"id": "new-policy", "evidence_type": "document_derived_term",
                      "summary": "Equipment maintenance", "source_pointer": "new-policy.md"}],
    })
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset())):
        for query in ("billable labor", "billable", "labor"):
            assert search_document_evidence(store, tenant_id="demo", query=query)["matches"] == []
    store.close()
