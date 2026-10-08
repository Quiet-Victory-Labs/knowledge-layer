"""Source IO begins only after durable attribution; retired reads stay attributable."""

import json

import pytest
from sqlalchemy import select

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import RefreshFenceLost, refresh_fence
from knowledge_layer.source_io_audit import source_io_attempt
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeStore


@pytest.fixture
def queued(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'audit.db'}")
    source = store.create_connection("demo", kind="local_files", name="Private", config={})
    store.grant_source_access("demo", source["id"], principal_type="user", principal_id="verified-fixture-user", role="manage")
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["verified-fixture-user"], "allowed_groups": []}]}))
    store.access_enforced = True
    queue = RefreshQueue(store)
    with principal_scope(Principal("verified-fixture-user", frozenset(), frozenset({"knowledge.sources.manage"}))):
        queue.enqueue("demo", source["id"])
    store.access_enforced = False  # trusted worker, still checking the recorded requester
    job = queue.claim("fixture-worker")
    yield store, queue, job
    store.close()


def native_events(store):
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(store.audit_events).where(
            store.audit_events.c.event_type.like("source.native_read_%"))
            .order_by(store.audit_events.c.occurred_at)).mappings()]


def test_start_is_committed_before_read_and_outcome_is_attributed(queued):
    store, _, job = queued
    with refresh_fence(job["id"], job["worker_id"], job["attempt"]), source_io_attempt(store, job):
        events = native_events(store)
        assert len(events) == 1 and events[0]["event_type"] == "source.native_read_started"
    started, finished = native_events(store)
    assert started["actor"] == finished["actor"] == "verified-fixture-user"
    assert started["details"]["read_id"] == finished["details"]["read_id"]
    assert finished["details"]["outcome"] == "returned"
    assert finished["details"]["job_id"] == job["id"]
    assert "config" not in json.dumps(finished)


def test_expired_or_cancelled_attempt_cannot_start_source_read(queued):
    store, queue, job = queued
    queue.cancel("demo", job["id"])
    with refresh_fence(job["id"], job["worker_id"], job["attempt"]), pytest.raises(RefreshFenceLost), source_io_attempt(store, job):
        pytest.fail("A cancelled claim must not reach source IO")
    assert native_events(store) == []


def test_failed_read_retains_outcome_without_source_text(queued):
    store, _, job = queued
    with refresh_fence(job["id"], job["worker_id"], job["attempt"]), pytest.raises(RuntimeError, match="private-source-text"), source_io_attempt(store, job):
        raise RuntimeError("private-source-text")
    assert native_events(store)[-1]["details"]["outcome"] == "failed"
    assert "private-source-text" not in json.dumps(native_events(store))


def test_audit_insert_failure_prevents_source_io(queued, monkeypatch):
    store, _, job = queued
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(store, "_audit", unavailable)
    with refresh_fence(job["id"], job["worker_id"], job["attempt"]), pytest.raises(RuntimeError, match="ledger unavailable"), source_io_attempt(store, job):
        pytest.fail("Audit failure must refuse source IO")
    assert native_events(store) == []
