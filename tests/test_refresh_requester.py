"""Queued work loses authority after grant or operator-subject withdrawal."""
import json

import pytest
from sqlalchemy import select, update

from knowledge_layer import worker
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import RefreshFenceLost, assert_refresh_fence, refresh_fence
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.source_policy import SourcePolicy, source_identity
from knowledge_layer.store import KnowledgeStore


def fixture(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'requester.db'}")
    source = store.create_connection("demo", kind="local_files", name="Requester source", config={})
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="owner", role="manage")
    job = RefreshQueue(store).enqueue("demo", source["id"])
    with store.engine.begin() as conn:
        conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == job["id"]).values(requested_by="owner"))
    policy = {"version": 1, "sources": [{"tenant_id": "demo", "kind": source["kind"],
        "selector": json.loads(source_identity(source["kind"], source["config"]))[1],
        "allowed_subjects": ["owner"], "allowed_groups": []}]}
    store.source_policy = SourcePolicy.from_json(json.dumps(policy))
    claim = RefreshQueue(store).claim("worker")
    return store, source, claim, policy


@pytest.mark.parametrize("withdraw", ["grant", "policy"])
def test_manual_job_refuses_revoked_requester_before_connector_io(tmp_path, monkeypatch, withdraw):
    store, source, job, policy = fixture(tmp_path)
    try:
        if withdraw == "grant":
            store.revoke_source_access("demo", source["id"], principal_type="user", principal_id="owner")
        else:
            policy["sources"][0]["allowed_subjects"] = []
            policy["sources"][0]["allowed_groups"] = ["old-owner-group"]
            store.source_policy = SourcePolicy.from_json(json.dumps(policy))
        monkeypatch.setattr(worker, "_store", lambda: store)
        monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files")
        monkeypatch.setattr(store, "refresh_connection", lambda *_: pytest.fail("revoked source IO"))
        assert worker._execute(job["id"], "worker", job["attempt"]) == 2
    finally:
        store.close()


def test_grant_revocation_during_connector_refuses_snapshot_commit(tmp_path):
    store, source, job, _ = fixture(tmp_path)
    try:
        with refresh_fence(job["id"], "worker", job["attempt"]):
            with store.engine.begin() as conn:
                assert_refresh_fence(store, conn, "demo", source["id"])
            store.revoke_source_access("demo", source["id"], principal_type="user", principal_id="owner")
            with pytest.raises(RefreshFenceLost):
                store.save_snapshot("demo", source["id"], {"snapshot_id": "revoked-result", "status": "ready"}, source_refresh=True)
        with store.engine.connect() as conn:
            assert conn.execute(select(store.snapshots)).first() is None
    finally:
        store.close()


def test_current_direct_requester_retains_manual_refresh_authority(tmp_path):
    store, source, job, _ = fixture(tmp_path)
    try:
        assert RefreshSchedules(store).job_allowed(job, enabled_kinds={"local_files"})
        with refresh_fence(job["id"], "worker", job["attempt"]), store.engine.begin() as conn:
            assert_refresh_fence(store, conn, "demo", source["id"])
    finally:
        store.close()
