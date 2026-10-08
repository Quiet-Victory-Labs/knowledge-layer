"""Durable source jobs preserve access, retries, leases, and cancellation."""

from __future__ import annotations

import json

from sqlalchemy import update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeStore, utc_now


def test_refresh_job_lifecycle_and_source_grants(tmp_path):
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    store = KnowledgeStore(database_url=url, access_enforced=True)
    store.source_admin_group = "stewards"
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["owner"], "allowed_groups": [],
    }]}))
    owner = Principal("owner", frozenset({"stewards"}))
    viewer = Principal("viewer", frozenset())
    queue = RefreshQueue(store)
    with principal_scope(owner):
        source = store.create_connection("demo", kind="local_files", name="Jobs", config={})
        source_id = source["id"]
        first = queue.enqueue("demo", source_id)
        assert first["status"] == "queued"
        assert queue.enqueue("demo", source_id)["id"] == first["id"]
    with principal_scope(viewer):
        assert queue.get("demo", first["id"]) is None

    operator = KnowledgeStore(database_url=url, initialize_schema=False)
    operator.grant_source_access("demo", source_id, principal_type="user", principal_id="viewer", role="read")
    with principal_scope(viewer):
        assert queue.get("demo", first["id"])["status"] == "queued"

    worker_queue = RefreshQueue(operator)
    claimed = worker_queue.claim("worker-one")
    assert claimed["id"] == first["id"] and claimed["attempt"] == 1
    assert worker_queue.heartbeat(first["id"], "worker-one", attempt=claimed["attempt"]) is True
    assert worker_queue.finish(first["id"], "worker-one", attempt=claimed["attempt"], error_code="curation_failed") is True
    assert worker_queue.get("demo", first["id"])["status"] == "retry_wait"
    with operator.engine.begin() as connection:
        connection.execute(update(operator.refresh_jobs).where(
            operator.refresh_jobs.c.id == first["id"],
        ).values(next_run_at=utc_now()))
    retry = worker_queue.claim("worker-two")
    assert retry["id"] == first["id"] and retry["attempt"] == 2
    assert worker_queue.finish(first["id"], "worker-one", attempt=claimed["attempt"], result_snapshot_id="wrong") is False
    assert worker_queue.finish(first["id"], "worker-two", attempt=retry["attempt"], result_snapshot_id="snapshot-good") is True
    assert worker_queue.get("demo", first["id"])["status"] == "succeeded"
    with principal_scope(owner):
        second = queue.enqueue("demo", source_id)
        assert second["id"] != first["id"]
        cancelled = queue.cancel("demo", second["id"])
        assert cancelled["status"] == "cancelled"
    assert worker_queue.claim("worker-three") is None
    events = operator.audit_page("demo", connection_id=source_id)
    assert [event["event_type"] for event in events].count("source.refresh_requested") == 2
    assert [event["event_type"] for event in events].count("source.refresh_attempt") == 2
    assert [event["event_type"] for event in events].count("source.refresh_cancel_requested") == 1
    operator.close()
    store.close()


def test_expired_lease_is_reclaimed_and_cancel_request_finishes(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'lease.db'}")
    source = store.create_connection("demo", kind="local_files", name="Lease", config={})
    queue = RefreshQueue(store)
    job = queue.enqueue("demo", source["id"])
    assert queue.claim("lost-worker")["attempt"] == 1
    with store.engine.begin() as connection:
        connection.execute(update(store.refresh_jobs).where(
            store.refresh_jobs.c.id == job["id"],
        ).values(lease_until="2000-01-01T00:00:00Z"))
    recovered = queue.claim("new-worker")
    assert recovered["attempt"] == 2
    assert queue.heartbeat(job["id"], "lost-worker", attempt=1) is False
    assert queue.cancel("demo", job["id"])["cancel_requested"] is True
    assert queue.heartbeat(job["id"], "new-worker", attempt=recovered["attempt"]) is False
    assert queue.finish(job["id"], "new-worker", attempt=recovered["attempt"], cancelled=True) is True
    assert queue.get("demo", job["id"])["status"] == "cancelled"
    store.close()
