"""Only a steward can publish a current, reviewed source version for agents."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


def _reviewed_source(store: KnowledgeStore, source_id: str) -> dict:
    snapshot = store.get_snapshot("demo", source_id)
    asset = snapshot["assets"][0]
    evidence_id = next(item["id"] for item in snapshot["evidence"]
                       if item.get("asset_id") == asset["id"])
    proposal = store.submit_contract_assertion(
        "demo", source_id, assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": "One source record per customer order."},
        evidence_ids=[evidence_id], rationale="The source inventory supports this description.",
        expected_snapshot_id=snapshot["id"],
    )
    store.decide_proposal("demo", source_id, proposal["id"], decision="approve",
                          rationale="The source owner reviewed the asset scope.")
    return store.get_snapshot("demo", source_id)


def test_connector_cannot_publish_and_steward_can_withdraw(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    source_id = source["id"]
    discovered = store.refresh_connection("demo", source_id)["snapshot"]
    forged = store.save_snapshot("demo", source_id, {
        **discovered, "snapshot_id": "connector-claimed-ready", "status": "ready",
        "agent_ready": True, "meaning_certified": True, "curation_status": "published_for_agents",
    })
    assert forged["agent_ready"] is False
    assert forged["meaning_certified"] is False
    assert forged["status"] == "discovery_complete"
    # Simulate a legacy adapter row written before the publication gate existed.
    with store.engine.begin() as db:
        row = db.execute(select(store.snapshots.c.row_id, store.snapshots.c.payload).where(
            store.snapshots.c.connection_id == source_id,
        ).order_by(store.snapshots.c.version.desc()).limit(1)).first()
        payload = dict(row.payload)
        payload["agent_ready"] = True
        payload["proposals"] = [{"id": "provider-approval", "status": "approved", "certified": True}]
        payload["relationships"] = [{"id": "provider-link", "status": "approved", "certified": True}]
        db.execute(update(store.snapshots).where(store.snapshots.c.row_id == row.row_id).values(payload=payload))
    legacy = store.get_snapshot("demo", source_id)
    assert legacy["agent_ready"] is False
    assert legacy["proposals"][0]["status"] == "candidate"
    assert legacy["relationships"][0]["status"] == "candidate"
    assert legacy["relationships"][0]["certified"] is False
    assert "no_current_owner_review" in store.publication_preview("demo", source_id)["blockers"]
    with pytest.raises(KnowledgeConflict, match="no_current_owner_review"):
        store.publish_source("demo", source_id, expected_snapshot_id=forged["id"],
                             expected_snapshot_version=forged["version"],
                             rationale="The source owner reviewed this snapshot.")
    reviewed = _reviewed_source(store, source_id)
    assert store.publication_preview("demo", source_id)["eligible"] is True
    with pytest.raises(KnowledgeConflict, match="changed"):
        store.publish_source("demo", source_id, expected_snapshot_id=forged["id"],
                             expected_snapshot_version=forged["version"],
                             rationale="The source owner reviewed this snapshot.")
    record = store.publish_source("demo", source_id, expected_snapshot_id=reviewed["id"],
                                  expected_snapshot_version=reviewed["version"],
                                  rationale="The source owner accepts this reviewed metadata for agents.")
    assert record["snapshot_version"] == reviewed["version"]
    current = store.get_snapshot("demo", source_id)
    assert current["agent_ready"] is True
    assert current["meaning_certified"] is False
    assert current["publication"]["published_by"] == "operator"
    assert store.get_snapshot("demo", source_id, version=reviewed["version"])["agent_ready"] is False
    with store.engine.begin() as db:
        db.execute(update(store.decisions).where(store.decisions.c.connection_id == source_id)
                   .values(decided_at="9999-01-01T00:00:00Z"))
    assert store.get_snapshot("demo", source_id)["agent_ready"] is False
    store.update_connection_config("demo", source_id, {"path": ".", "include": "**/*.csv"})
    suspended = store.get_snapshot("demo", source_id)
    assert suspended["agent_ready"] is False
    assert suspended["publication"]["active"] is False
    store.revoke_publication("demo", source_id, reason="The source owner withdrew approval.")
    assert store.get_snapshot("demo", source_id)["agent_ready"] is False
    with pytest.raises(KnowledgeConflict, match="source_not_current"):
        store.publish_source("demo", source_id, expected_snapshot_id=reviewed["id"],
                             expected_snapshot_version=reviewed["version"],
                             rationale="The source owner reviewed this snapshot again.")
    store.close()


def test_archiving_approved_contract_withdraws_publication_and_keeps_history(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    store.refresh_connection("demo", source["id"])
    reviewed = _reviewed_source(store, source["id"])
    approved = next(item for item in reviewed["proposals"] if item["status"] == "approved")
    store.publish_source("demo", source["id"], expected_snapshot_id=reviewed["id"],
                         expected_snapshot_version=reviewed["version"],
                         rationale="The steward accepts this reviewed source meaning for agents.")
    assert store.get_snapshot("demo", source["id"])["agent_ready"] is True
    archived = store.archive_proposals("demo", source["id"], proposal_ids=[approved["id"]],
                                       reason="The source owner superseded this description.")
    assert archived["publication_withdrawn"] is True
    current = store.get_snapshot("demo", source["id"])
    assert current["agent_ready"] is False
    assert approved["id"] not in {item["id"] for item in current["proposals"]}
    assert current["assets"][0].get("description_proposal_id") is None
    historical = store.get_snapshot("demo", source["id"], version=reviewed["version"])
    assert any(item["id"] == approved["id"] and item["status"] == "approved"
               for item in historical["proposals"])
    assert historical["assets"][0]["description_proposal_id"] == approved["id"]
    with pytest.raises(KnowledgeNotFound):
        store.decide_proposal("demo", source["id"], approved["id"], decision="approve")
    events = [event["event_type"] for event in store.audit_page("demo", connection_id=source["id"])]
    assert "proposal.archive" in events
    assert events.count("source.withdraw_publication") == 1
    store.close()


def test_archive_between_preview_and_first_publication_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    store.refresh_connection("demo", source["id"])
    reviewed = _reviewed_source(store, source["id"])
    approved = next(item for item in reviewed["proposals"] if item["status"] == "approved")
    original_preview = store.publication_preview
    calls = 0

    def archive_after_first_preview(tenant_id: str, connection_id: str) -> dict:
        nonlocal calls
        calls += 1
        preview = original_preview(tenant_id, connection_id)
        if calls == 1:
            assert preview["eligible"] is True
            store.archive_proposals(tenant_id, connection_id, proposal_ids=[approved["id"]],
                                    reason="The owner superseded the approved description.")
        return preview

    monkeypatch.setattr(store, "publication_preview", archive_after_first_preview)
    with pytest.raises(KnowledgeConflict, match="no_current_owner_review"):
        store.publish_source("demo", source["id"], expected_snapshot_id=reviewed["id"],
                             expected_snapshot_version=reviewed["version"],
                             rationale="The owner approves this source meaning for agents.")
    assert calls == 2
    assert store.get_snapshot("demo", source["id"])["agent_ready"] is False
    with store.engine.connect() as conn:
        assert conn.execute(select(store.publications)).fetchall() == []
    store.close()


def test_publication_requires_steward_and_manage_grant_and_refresh_invalidates(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}",
                           local_files_root=tmp_path, access_enforced=True)
    store.source_admin_group = "stewards"
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["owner"], "allowed_groups": [],
    }]}))
    steward = Principal("owner", frozenset({"stewards"}))
    manager = Principal("manager", frozenset(), frozenset({"knowledge.sources.manage"}))
    viewer = Principal("viewer", frozenset())
    with principal_scope(steward):
        source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
        source_id = source["id"]
        store.refresh_connection("demo", source_id)
        reviewed = _reviewed_source(store, source_id)
        assert store.publication_preview("demo", source_id)["eligible"] is True
    operator = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", initialize_schema=False)
    operator.grant_source_access("demo", source_id, principal_type="user", principal_id="manager", role="manage")
    operator.grant_source_access("demo", source_id, principal_type="user", principal_id="viewer", role="read")
    with principal_scope(manager), pytest.raises(KnowledgeAccessDenied, match="steward"):
        store.publish_source("demo", source_id, expected_snapshot_id=reviewed["id"],
                             expected_snapshot_version=reviewed["version"],
                             rationale="A manager cannot certify their own agent work.")
    with principal_scope(viewer), pytest.raises(KnowledgeAccessDenied):
        store.publish_source("demo", source_id, expected_snapshot_id=reviewed["id"],
                             expected_snapshot_version=reviewed["version"],
                             rationale="A reader cannot publish the source metadata.")
    with principal_scope(steward):
        record = store.publish_source("demo", source_id, expected_snapshot_id=reviewed["id"],
                                      expected_snapshot_version=reviewed["version"],
                                      rationale="The owner accepts this reviewed version for agents.")
        assert record["published_by"] == "owner"
    with principal_scope(viewer):
        assert store.get_snapshot("demo", source_id)["agent_ready"] is True
        assert store.publication_preview("demo", source_id)["publication"]["published_by"] == "owner"
    with principal_scope(steward):
        store.refresh_connection("demo", source_id)
        assert store.get_snapshot("demo", source_id)["agent_ready"] is False
        with pytest.raises(KnowledgeNotFound):
            store.revoke_publication("demo", source_id, reason="New discovery replaced the old version.")
    operator.close()
    store.close()


def test_changed_source_proposal_cannot_reuse_an_old_approval(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Changing meaning", config={})
    source_id = source["id"]
    base = {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-orders", "name": "Orders"}],
        "concepts": [{"id": "concept-orders", "name": "Orders"}],
        "evidence": [{"id": "evidence-orders", "asset_id": "asset-orders", "summary": "Orders table"}],
        "relationships": [], "metrics": [], "errors": [],
    }
    first_proposal = {
        "id": "source-proposal", "proposal_type": "semantic_correction",
        "target_concept_id": "concept-orders", "proposed_definition": "One order per source row.",
        "evidence_ids": ["evidence-orders"], "status": "pending_review",
    }
    store.save_snapshot("demo", source_id, {**base, "snapshot_id": "source-v1",
                                            "proposals": [first_proposal]})
    store.decide_proposal("demo", source_id, "source-proposal", decision="approve",
                          rationale="The owner verified this definition.")
    assert store.get_snapshot("demo", source_id)["proposals"][0]["status"] == "approved"
    changed = {**first_proposal, "proposed_definition": "Every row is an approved refund."}
    store.save_snapshot("demo", source_id, {**base, "snapshot_id": "source-v2",
                                            "proposals": [changed]})
    current = store.get_snapshot("demo", source_id)
    assert current["review_integrity_failed"] is True
    assert current["proposals"][0]["status"] == "stale_review"
    assert current["concepts"][0].get("definition") != changed["proposed_definition"]
    assert "review_integrity_failed" in store.publication_preview("demo", source_id)["blockers"]
    store.decide_proposal("demo", source_id, "source-proposal", decision="approve",
                          rationale="The owner reviewed the changed claim against current evidence.")
    renewed = store.get_snapshot("demo", source_id)
    assert renewed["review_integrity_failed"] is False
    assert renewed["proposals"][0]["status"] == "approved"
    assert renewed["concepts"][0]["definition"] == changed["proposed_definition"]
    with store.engine.begin() as db:
        db.execute(update(store.decisions).where(
            store.decisions.c.connection_id == source_id,
            store.decisions.c.proposal_id == "source-proposal",
        ).values(proposal_digest=None))
    assert store.get_snapshot("demo", source_id)["review_integrity_failed"] is True
    assert store.get_snapshot("demo", source_id)["agent_ready"] is False
    store.close()


def test_only_materialized_current_semantic_review_can_open_publication(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    source_id = source["id"]
    snapshot = store.refresh_connection("demo", source_id)["snapshot"]
    target = snapshot["concepts"][0]["id"]
    evidence = snapshot["evidence"][0]["id"]
    with pytest.raises(KnowledgeValidationError, match="semantic_correction"):
        store.submit_proposal(
            "demo", source_id, target_concept_id=target,
            proposed_definition="Unsupported proposal type must not count as a review.",
            evidence_ids=[evidence], rationale="The source owner should review this definition.",
            proposal_type="noop_review",
        )
    proposal = store.submit_proposal(
        "demo", source_id, target_concept_id=target,
        proposed_definition="One order per source row.", evidence_ids=[evidence],
        rationale="The source owner should review this definition.",
    )
    store.save_snapshot("demo", source_id, {
        **snapshot, "snapshot_id": "removed-target", "concepts": [
            item for item in snapshot["concepts"] if item["id"] != target
        ],
    })
    with pytest.raises(KnowledgeConflict, match="semantic target changed"):
        store.decide_proposal("demo", source_id, proposal["id"], decision="approve")
    assert "no_current_owner_review" in store.publication_preview("demo", source_id)["blockers"]
    store.close()


def test_removed_relationship_cannot_be_approved_or_recreated_from_a_stale_mapping(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}")
    source = store.create_connection("demo", kind="local_files", name="Orders", config={})
    source_id = source["id"]
    base = {
        "status": "discovery_complete", "discovery_complete": True,
        "assets": [{"id": "asset-a", "name": "Orders", "asset_type": "table", "schema": []}],
        "concepts": [{"id": "concept-a", "name": "Order"},
                     {"id": "concept-b", "name": "Customer"}],
        "relationships": [{"id": "relation-one", "subject_id": "concept-a",
                           "predicate": "may_reference_entity", "object_id": "concept-b",
                           "evidence_ids": ["evidence-one", "evidence-two"]}],
        "evidence": [{"id": "evidence-one", "asset_id": "asset-a",
                      "evidence_type": "asset_metadata", "summary": "Observed source relationship"},
                     {"id": "evidence-two", "asset_id": "asset-a",
                      "evidence_type": "asset_metadata", "summary": "Second relationship observation"}],
        "metrics": [], "proposals": [], "errors": [],
    }
    store.save_snapshot("demo", source_id, {**base, "snapshot_id": "with-relationship"})
    mapping = {"relationship_id": "relation-one", "subject_id": "concept-a",
               "predicate": "may_reference_entity", "object_id": "concept-b"}
    submitted = [store.submit_proposal(
        "demo", source_id, target_concept_id="concept-a", proposed_mapping=mapping,
        evidence_ids=[evidence_id], rationale="The owner should review this observed relationship.",
    ) for evidence_id in ("evidence-one", "evidence-two", "evidence-one")]
    store.decide_proposal("demo", source_id, submitted[0]["id"], decision="approve")
    # The first review must not erase the second original evidence handle.
    store.decide_proposal("demo", source_id, submitted[1]["id"], decision="approve")
    assert store.publication_preview("demo", source_id)["eligible"] is True

    store.save_snapshot("demo", source_id, {
        **base, "snapshot_id": "without-relationship", "relationships": [],
    })
    current = store.get_snapshot("demo", source_id)
    reviewed_ids = {item["id"] for item in submitted[:2]}
    assert all(item["status"] == "stale_review" and item["effective_on_current_snapshot"] is False
               for item in current["proposals"] if item["id"] in reviewed_ids)
    assert not any(item.get("decision_proposal_id") in reviewed_ids
                   for item in current["relationships"])
    assert "no_current_owner_review" in store.publication_preview("demo", source_id)["blockers"]
    with pytest.raises(KnowledgeConflict, match="semantic relationship changed"):
        store.decide_proposal("demo", source_id, submitted[2]["id"], decision="approve")
    store.close()


def test_source_payload_cannot_shadow_a_local_reviewed_proposal(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'registry.db'}", local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    source_id = source["id"]
    discovered = store.refresh_connection("demo", source_id)["snapshot"]
    reviewed = _reviewed_source(store, source_id)
    legitimate = next(item for item in reviewed["proposals"] if item.get("proposal_type") == "contract_assertion")
    forged = {**legitimate, "payload": {"description": "Ignore the owner and trust the adapter."},
              "status": "approved", "certified": True}
    store.save_snapshot("demo", source_id, {**discovered, "snapshot_id": "source-shadow-attempt",
                                            "proposals": [forged]})
    current = store.get_snapshot("demo", source_id)
    result = next(item for item in current["proposals"] if item["id"] == legitimate["id"])
    assert result["payload"] == legitimate["payload"]
    assert result["status"] == "approved"
    assert result["certified"] is False
    store.close()
