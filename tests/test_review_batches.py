"""A human accepts exactly reviewed claims; refresh and failed commits cannot broaden it."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.review_batches import accept_review_batch, preview_review_batch
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


@pytest.fixture(params=["file", "memory"])
def drafts(tmp_path, request):
    (tmp_path / "orders.csv").write_text("order_id,total\nO-1,12\n", encoding="utf-8")
    url = "sqlite:///:memory:" if request.param == "memory" else f"sqlite:///{tmp_path / 'registry.db'}"
    store = KnowledgeStore(database_url=url, local_files_root=tmp_path)
    source = store.create_connection("demo", kind="local_files", name="Orders", config={"path": "."})
    snapshot = store.refresh_connection("demo", source["id"])["snapshot"]
    asset = snapshot["assets"][0]
    evidence_id = next(item["id"] for item in snapshot["evidence"]
                       if item.get("asset_id") == asset["id"])
    proposals = [store.submit_contract_assertion(
        "demo", source["id"], assertion_kind="dataset_description", asset_id=asset["id"],
        payload={"description": description}, evidence_ids=[evidence_id],
        rationale="Observed source records support this exact description.",
        expected_snapshot_id=snapshot["id"],
    ) for description in ("Observed order records.", "One source row describes an order.")]
    try:
        yield store, source["id"], snapshot, [item["id"] for item in proposals]
    finally:
        store.close()


def _arguments(drafts):
    _, _, snapshot, ids = drafts
    return {"proposal_ids": ids, "expected_snapshot_id": snapshot["id"],
            "expected_snapshot_version": snapshot["version"]}


def _acceptance(drafts):
    store, cid, _, _ = drafts
    preview = preview_review_batch(store, "demo", cid, **_arguments(drafts))
    return {**_arguments(drafts), "expected_review_digest": preview["review_digest"],
            "rationale": "I reviewed the selected claims and their supporting evidence."}


def _counts(store, cid):
    with store.engine.connect() as conn:
        return tuple(conn.execute(select(func.count()).select_from(table).where(
            table.c.connection_id == cid)).scalar_one()
            for table in (store.decisions, store.snapshots, store.audit_events))


def test_batch_records_exact_selection_and_one_version_then_publishes(drafts):
    store, cid, snapshot, ids = drafts
    preview = preview_review_batch(store, "demo", cid, **_arguments(drafts))
    assert [item["id"] for item in preview["proposals"]] == ids
    assert preview["evidence"] and "Observed order records." in str(preview)
    before = _counts(store, cid)
    result = accept_review_batch(store, "demo", cid, **_acceptance(drafts), publish=True)
    assert result["status"] == "published" and result["reviewed_proposal_ids"] == ids
    assert result["snapshot_version"] == snapshot["version"] + 1
    current = store.get_snapshot("demo", cid)
    assert current["id"] == result["snapshot_id"] and current["agent_ready"] is True
    assert all(item["status"] == "approved" for item in current["proposals"] if item["id"] in ids)
    after = _counts(store, cid)
    assert after[:2] == (before[0] + 2, before[1] + 1)
    assert current["meaning_certified"] is False


def test_permission_enforced_public_batch_review_keeps_native_resource_lineage(drafts):
    import json
    from datetime import UTC, datetime, timedelta

    from knowledge_layer.source_permissions import digest, manifest_digest
    from knowledge_layer.source_policy import IDENTITY_FIELDS, SourcePolicy, source_identity

    store, cid, snapshot, _ = drafts
    owner = Principal("steward", frozenset({"stewards"}), frozenset({"knowledge.sources.manage"}))
    store.grant_source_access("demo", cid, principal_type="user", principal_id=owner.subject, role="manage")
    config = store.get_connection("demo", cid)["config"]
    selector = {key: value for key, value in config.items() if key in IDENTITY_FIELDS["local_files"]}
    store.source_admin_group = "stewards"
    native_pin = snapshot["source_permission_manifest_digest"]
    now = datetime.now(UTC) - timedelta(seconds=1)
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 2, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": selector,
        "allowed_subjects": [owner.subject], "allowed_groups": []}], "metadata_permissions": {
        "enforce": True, "trusted_native_exporters": [], "attestations": [{
            "tenant_id": "demo", "kind": "local_files", "selector": selector,
            "source_identity_digest": digest(source_identity("local_files", selector)),
            "basis": "operator_partition", "authority": "fixture-operator", "revision": "v1", "state": "complete",
            "checked_at": now.isoformat(), "expires_at": (now + timedelta(seconds=600)).isoformat(),
            "allowed_subjects": [owner.subject], "allowed_groups": [], "manifest_digests": [native_pin],
            "resource_manifest_digest": manifest_digest([native_pin])}]}}))
    store.access_enforced = True
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store))
    with principal_scope(owner), TestClient(app) as client:
        preview = client.post(f"/api/knowledge/connections/{cid}/review-batch/preview", json=_arguments(drafts))
        assert preview.status_code == 200
        body = {**_arguments(drafts), "expected_review_digest": preview.json()["review_digest"],
                "rationale": "I reviewed all selected definitions and their exact evidence.", "publish": True}
        accepted = client.post(f"/api/knowledge/connections/{cid}/review-batch/accept", json=body)
        assert accepted.status_code == 200 and accepted.json()["status"] == "published"
        reviewed = store.get_snapshot("demo", cid)
        assert reviewed["source_permission_manifest_digest"] == native_pin
        assert reviewed["agent_ready"] is True


def test_unselected_drafts_remain_pending_and_review_is_not_source_refresh(drafts):
    store, cid, _, ids = drafts
    arguments = {**_arguments(drafts), "proposal_ids": ids[:1]}
    preview = preview_review_batch(store, "demo", cid, **arguments)
    old_clock = store.get_connection("demo", cid)["last_complete_refresh_at"]
    result = accept_review_batch(store, "demo", cid, **arguments,
                               expected_review_digest=preview["review_digest"],
                               rationale="I reviewed only this selected source description.")
    current = store.get_snapshot("demo", cid)
    assert result["status"] == "accepted" and current["agent_ready"] is False
    assert next(item for item in current["proposals"] if item["id"] == ids[1])["status"] == "pending_review"
    assert result["remaining_pending_count"] == preview["remaining_pending_count"]
    assert store.get_connection("demo", cid)["last_complete_refresh_at"] == old_clock


def test_changed_claim_or_evidence_refuses_entire_selection(drafts):
    store, cid, _, ids = drafts
    arguments = _acceptance(drafts)
    with store.engine.begin() as conn:
        conn.execute(update(store.contract_proposals).where(store.contract_proposals.c.id == ids[1]).values(
            payload={"description": "Changed after the human previewed the claim."},
        ))
    before = _counts(store, cid)
    with pytest.raises(KnowledgeConflict, match="claims or evidence changed"):
        accept_review_batch(store, "demo", cid, **arguments)
    assert _counts(store, cid) == before


def test_new_snapshot_refuses_review_without_partial_approval(drafts):
    store, cid, snapshot, _ = drafts
    arguments = _acceptance(drafts)
    store.save_snapshot("demo", cid, {**snapshot, "snapshot_id": "new-source-version"})
    before = _counts(store, cid)
    with pytest.raises(KnowledgeConflict, match="snapshot changed"):
        accept_review_batch(store, "demo", cid, **arguments)
    assert _counts(store, cid) == before


def test_failed_snapshot_write_rolls_back_decisions_and_audit(drafts, monkeypatch):
    store, cid, _, _ = drafts
    arguments = _acceptance(drafts)
    original = store._save_snapshot_in_transaction

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Interrupted before commit")

    monkeypatch.setattr(store, "_save_snapshot_in_transaction", fail)
    before = _counts(store, cid)
    with pytest.raises(RuntimeError, match="Interrupted"):
        accept_review_batch(store, "demo", cid, **arguments)
    assert _counts(store, cid) == before


def test_missing_evidence_in_later_selection_cannot_approve_first(drafts):
    store, cid, _, ids = drafts
    arguments = _acceptance(drafts)
    with store.engine.begin() as conn:
        conn.execute(update(store.contract_proposals).where(store.contract_proposals.c.id == ids[1]).values(
            evidence_ids=["removed-evidence"],
        ))
    before = _counts(store, cid)
    with pytest.raises(KnowledgeValidationError, match="evidence handles"):
        accept_review_batch(store, "demo", cid, **arguments)
    assert _counts(store, cid) == before


def test_manager_can_review_but_cannot_use_combined_steward_publication(drafts):
    store, cid, _, _ = drafts
    arguments = _acceptance(drafts)
    store.grant_source_access("demo", cid, principal_type="user", principal_id="builder", role="manage")
    store.access_enforced = True
    with principal_scope(Principal("builder", frozenset(), frozenset({"knowledge.sources.manage"}))):
        before = _counts(store, cid)
        with pytest.raises(KnowledgeAccessDenied, match="steward"):
            accept_review_batch(store, "demo", cid, **arguments, publish=True)
        assert _counts(store, cid) == before
        assert accept_review_batch(store, "demo", cid, **arguments)["status"] == "accepted"


def test_grant_revocation_after_preview_refuses_without_writes(drafts):
    store, cid, _, _ = drafts
    arguments = _acceptance(drafts)
    store.access_enforced = True
    with principal_scope(Principal("reader", frozenset(), frozenset({"knowledge.sources.manage"}))):
        before = _counts(store, cid)
        with pytest.raises(KnowledgeNotFound):
            accept_review_batch(store, "demo", cid, **arguments)
        assert _counts(store, cid) == before


def test_publication_failure_reports_completed_review_explicitly(drafts, monkeypatch):
    store, cid, _, ids = drafts
    arguments = _acceptance(drafts)

    def blocked(*_args, **_kwargs):
        raise KnowledgeConflict("Publication blocked: refresh_active")

    monkeypatch.setattr(store, "publish_source", blocked)
    result = accept_review_batch(store, "demo", cid, **arguments, publish=True)
    assert result["status"] == "accepted_publication_blocked"
    assert "refresh_active" in result["publication_error"]
    assert all(item["status"] == "approved" for item in store.get_snapshot("demo", cid)["proposals"]
               if item["id"] in ids)


def test_http_batches_are_human_only_and_validate_exact_selection(drafts):
    store, cid, _, _ = drafts
    app = FastAPI()
    app.include_router(create_knowledge_router(store=store))
    with TestClient(app) as client:
        path = f"/api/knowledge/connections/{cid}/review-batch"
        preview = client.post(path + "/preview", json=_arguments(drafts))
        assert preview.status_code == 200
        bad = {**_arguments(drafts), "proposal_ids": []}
        assert client.post(path + "/preview", json=bad).status_code == 422
        assert client.post(path + "/accept", json={**_acceptance(drafts), "publish": "true"}).status_code == 422
        assert client.post(path + "/accept", json=_acceptance(drafts)).status_code == 200
    with TestClient(build_agent_api(store)) as agent:
        assert agent.post(f"/sources/{cid}/review-batch/accept", json={}).status_code == 404


def test_preview_budget_refuses_instead_of_truncating(drafts, monkeypatch):
    from knowledge_layer import review_batches

    store, cid, _, _ = drafts
    monkeypatch.setattr(review_batches, "MAX_PREVIEW_BYTES", 100)
    with pytest.raises(KnowledgeValidationError, match="select fewer"):
        preview_review_batch(store, "demo", cid, **_arguments(drafts))
