"""Paged source summaries retain single-source governance state with bounded SQL work."""

from __future__ import annotations

from sqlalchemy import event, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.store import KnowledgeStore


def test_fifty_source_page_bounds_sql_and_matches_individual_snapshots(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source_ids = [store.create_connection("demo", kind="local_files", name=f"Source {index:02d}", config={})["id"]
                  for index in range(50)]
    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", record)
    empty_page = store.page_connections("demo", limit=50)
    assert len(empty_page["connections"]) == 50
    assert len(statements) <= 6
    statements.clear()
    event.remove(store.engine, "before_cursor_execute", record)

    for source_id in source_ids:
        store.save_snapshot("demo", source_id, {
            "status": "discovery_complete", "discovery_complete": True,
            "assets": [{"id": "asset-1", "name": "Orders"}],
            "evidence": [{"id": "evidence-1", "asset_id": "asset-1", "summary": "Schema"}],
        })
    expected = {source_id: store.get_connection("demo", source_id) for source_id in source_ids}
    event.listen(store.engine, "before_cursor_execute", record)
    page = store.page_connections("demo", limit=50)
    event.remove(store.engine, "before_cursor_execute", record)
    assert len(statements) <= 25
    assert {row["id"]: row for row in page["connections"]} == expected
    store.close()


def test_batch_projection_preserves_publication_archive_and_grants(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    approved = store.create_connection("demo", kind="local_files", name="Approved", config={})
    candidate = store.create_connection("demo", kind="local_files", name="Candidate", config={})
    hidden = store.create_connection("demo", kind="local_files", name="Hidden", config={})
    approved_snapshot = store.save_snapshot("demo", approved["id"], {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-1", "name": "Orders"}],
        "evidence": [{"id": "evidence-1", "asset_id": "asset-1", "summary": "Schema"}],
    })
    assertion = store.submit_contract_assertion(
        "demo", approved["id"], assertion_kind="dataset_description", asset_id="asset-1",
        payload={"description": "One row per order."}, evidence_ids=["evidence-1"],
        rationale="The source inventory supports this description.",
        expected_snapshot_id=approved_snapshot["id"],
    )
    store.decide_proposal("demo", approved["id"], assertion["id"], decision="approve",
                          rationale="The source owner reviewed the asset scope.")
    reviewed = store.get_snapshot("demo", approved["id"])
    store.publish_source("demo", approved["id"], expected_snapshot_id=reviewed["id"],
                         expected_snapshot_version=reviewed["version"],
                         rationale="The owner approves this source meaning for agents.")

    candidate_snapshot = store.save_snapshot("demo", candidate["id"], {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-2", "name": "Customers"}],
        "concepts": [{"id": "concept-2", "name": "Customer"}],
        "evidence": [{"id": "evidence-2", "asset_id": "asset-2", "summary": "Schema"}],
        "proposals": [{"id": "provider-claimed", "status": "approved", "certified": True}],
    })
    local = store.submit_proposal(
        "demo", candidate["id"], target_concept_id="concept-2",
        proposed_definition="A distinct customer in the source system.",
        evidence_ids=["evidence-2"], rationale="The schema suggests one customer per key.",
        expected_snapshot_id=candidate_snapshot["id"],
    )
    with store.engine.begin() as conn:
        row = conn.execute(select(store.snapshots.c.row_id, store.snapshots.c.payload).where(
            store.snapshots.c.connection_id == candidate["id"],
        )).first()
        payload = dict(row.payload)
        payload["proposals"].append(local)
        conn.execute(update(store.snapshots).where(store.snapshots.c.row_id == row.row_id)
                     .values(payload=payload))
    store.archive_proposals("demo", candidate["id"], proposal_ids=[local["id"]],
                            reason="The owner superseded this draft proposal.")
    for source_id in (approved["id"], candidate["id"]):
        store.grant_source_access("demo", source_id, principal_type="user",
                                  principal_id="reader", role="read")
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset())):
        page = store.page_connections("demo", limit=10)
        assert page["total"] == 2
        assert {row["id"] for row in page["connections"]} == {approved["id"], candidate["id"]}
        assert all(row == store.get_connection("demo", row["id"]) for row in page["connections"])
        by_id = {row["id"]: row for row in page["connections"]}
        assert by_id[approved["id"]]["agent_ready"] is True
        assert by_id[candidate["id"]]["agent_ready"] is False
        assert local["id"] not in {item["id"] for item in by_id[candidate["id"]]["snapshot"]["proposals"]}
        assert store.get_connection("demo", hidden["id"]) is None
    store.close()
