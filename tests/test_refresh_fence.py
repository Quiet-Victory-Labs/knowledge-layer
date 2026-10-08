"""A stale refresh cannot write a newer source state or resurrect its lease."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import (
    RefreshFenceLost,
    assert_refresh_fence,
    record_refresh_snapshot,
    refresh_fence,
)
from knowledge_layer.store import KnowledgeStore, KnowledgeValidationError


@pytest.fixture
def claimed(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'fence.db'}")
    source = store.create_connection("demo", kind="local_files", name="Fenced", config={})
    store.save_snapshot("demo", source["id"], _payload("initial"), source_refresh=True)
    queue = RefreshQueue(store)
    queue.enqueue("demo", source["id"])
    job = queue.claim("worker")
    yield store, source["id"], queue, job
    store.close()


def _payload(snapshot_id):
    return {"snapshot_id": snapshot_id, "status": "discovery_complete", "discovery_complete": True,
            "assets": [], "concepts": [], "relationships": [], "proposals": [], "evidence": []}


def _job_update(store, job_id, **values):
    with store.engine.begin() as conn:
        conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == job_id).values(**values))


def _source_state(store, source_id):
    with store.engine.connect() as conn:
        source = conn.execute(select(store.connections).where(store.connections.c.id == source_id)).mappings().one()
        snapshots = conn.execute(select(store.snapshots.c.snapshot_id).where(
            store.snapshots.c.connection_id == source_id,
        ).order_by(store.snapshots.c.version)).scalars().all()
    return dict(source), snapshots


def test_current_fence_allows_snapshot_and_manual_refresh_remains_supported(claimed):
    store, source_id, _queue, job = claimed
    with refresh_fence(job["id"], "worker", job["attempt"]):
        result = store.save_snapshot("demo", source_id, _payload("current"), source_refresh=True)
    assert result["id"] == "current"
    # Outside the child context, a synchronous authorized operator can refresh.
    assert store.save_snapshot("demo", source_id, _payload("manual"), source_refresh=True)["id"] == "manual"


@pytest.mark.parametrize("mutation", [
    {"lease_until": "2000-01-01T00:00:00Z"}, {"lease_until": None},
    {"lease_until": "invalid"}, {"lease_until": "2099-01-01T00:00:00"},
    {"cancel_requested": True}, {"attempt": 2}, {"worker_id": "other"},
    {"status": "succeeded"},
])
def test_lost_fence_rolls_back_snapshot_and_clocks(claimed, mutation):
    store, source_id, _queue, job = claimed
    before = _source_state(store, source_id)
    _job_update(store, job["id"], **mutation)
    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        store.save_snapshot("demo", source_id, _payload("stale"), source_refresh=True)
    assert _source_state(store, source_id) == before


def test_fence_is_bound_to_exact_tenant_and_source_and_context_restores(claimed):
    store, source_id, _queue, job = claimed
    other = store.create_connection("demo", kind="local_files", name="Other", config={})["id"]
    with refresh_fence(job["id"], "worker", 1):
        for tenant, source in [("other-tenant", source_id), ("demo", other)]:
            with store.engine.begin() as conn, pytest.raises(RefreshFenceLost):
                assert_refresh_fence(store, conn, tenant, source)
        with refresh_fence(job["id"], "wrong", 1), store.engine.begin() as conn, pytest.raises(RefreshFenceLost):
            assert_refresh_fence(store, conn, "demo", source_id)
        with store.engine.begin() as conn:
            assert_refresh_fence(store, conn, "demo", source_id)
    with store.engine.begin() as conn:
        assert_refresh_fence(store, conn, "other-tenant", "manual-no-job")


def test_same_worker_reclaim_cannot_renew_finish_or_commit_old_attempt(claimed):
    store, source_id, queue, job = claimed
    _job_update(store, job["id"], lease_until="2000-01-01T00:00:00Z")
    assert queue.heartbeat(job["id"], "worker", attempt=1) is False
    assert queue.finish(job["id"], "worker", attempt=1, error_code="stale") is False
    recovered = queue.claim("worker")
    assert recovered["attempt"] == 2
    assert queue.worker_job(job["id"], "worker", attempt=1) is None
    assert queue.heartbeat(job["id"], "worker", attempt=1) is False
    assert queue.finish(job["id"], "worker", attempt=1, result_snapshot_id="stale") is False
    before = _source_state(store, source_id)
    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        store.save_snapshot("demo", source_id, _payload("stale"), source_refresh=True)
    assert _source_state(store, source_id) == before
    with refresh_fence(job["id"], "worker", 2):
        store.save_snapshot("demo", source_id, _payload("recovered"), source_refresh=True)
    assert queue.finish(job["id"], "worker", attempt=2, result_snapshot_id="recovered") is True
    assert queue.get("demo", job["id"])["result_snapshot_id"] == "recovered"


def test_recorded_result_is_generation_bound_transactional_and_reset_on_retry(claimed):
    store, source_id, queue, job = claimed
    with refresh_fence(job["id"], "worker", 1), store.engine.begin() as conn:
        record_refresh_snapshot(store, conn, "demo", source_id, "committed-one")
    assert queue.worker_job(job["id"], "worker", attempt=1)["result_snapshot_id"] == "committed-one"
    with pytest.raises(RuntimeError), refresh_fence(job["id"], "worker", 1), store.engine.begin() as conn:
        record_refresh_snapshot(store, conn, "demo", source_id, "rolled-back")
        raise RuntimeError("transaction failed")
    assert queue.worker_job(job["id"], "worker", attempt=1)["result_snapshot_id"] == "committed-one"
    _job_update(store, job["id"], lease_until="2000-01-01T00:00:00Z")
    recovered = queue.claim("worker")
    assert recovered["attempt"] == 2 and recovered["result_snapshot_id"] is None
    with refresh_fence(job["id"], "worker", 1), store.engine.begin() as conn, pytest.raises(RefreshFenceLost):
        record_refresh_snapshot(store, conn, "demo", source_id, "stale")
    assert queue.worker_job(job["id"], "worker", attempt=2)["result_snapshot_id"] is None
    with store.engine.begin() as conn:
        record_refresh_snapshot(store, conn, "demo", source_id, "manual-no-record")
    assert queue.worker_job(job["id"], "worker", attempt=2)["result_snapshot_id"] is None


@pytest.mark.parametrize("failure", [False, True])
def test_cancel_during_source_read_cannot_commit_or_mark_error(claimed, failure):
    store, source_id, queue, job = claimed
    before, snapshots = _source_state(store, source_id)

    def curator(*_args):
        queue.cancel("demo", job["id"])
        if failure:
            raise RuntimeError("private adapter details")
        return _payload("cancelled-read")

    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        store.refresh_connection("demo", source_id, curator=curator)
    after, observed = _source_state(store, source_id)
    assert observed == snapshots
    for field in ("status", "last_error", "last_refreshed_at", "last_complete_refresh_at"):
        assert after[field] == before[field]
    assert after["last_refresh_attempt_at"] is not None  # This began before cancellation.
    assert queue.finish(job["id"], "worker", attempt=1, cancelled=True) is True


@pytest.mark.parametrize("failure", [False, True])
def test_takeover_during_source_read_preserves_newer_snapshot_and_clocks(claimed, failure):
    store, source_id, queue, job = claimed
    committed = []

    def curator(*_args):
        _job_update(store, job["id"], lease_until="2000-01-01T00:00:00Z")
        recovered = queue.claim("new-worker")
        with refresh_fence(job["id"], "new-worker", recovered["attempt"]):
            store.save_snapshot("demo", source_id, _payload("newer"), source_refresh=True)
        committed.append(_source_state(store, source_id))
        if failure:
            raise RuntimeError("private adapter details")
        return _payload("stale")

    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        store.refresh_connection("demo", source_id, curator=curator)
    assert _source_state(store, source_id) == committed[0]


def test_expired_start_cannot_stamp_attempt_clock(claimed):
    store, source_id, _queue, job = claimed
    _job_update(store, job["id"], lease_until="2000-01-01T00:00:00Z")
    before = _source_state(store, source_id)
    reads = []
    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        store.refresh_connection("demo", source_id, curator=lambda *_args: reads.append(True))
    assert reads == [] and _source_state(store, source_id) == before


@pytest.mark.parametrize("lease", [None, "invalid", "2099-01-01T00:00:00", "2000-01-01T00:00:00Z"])
def test_parent_cannot_renew_or_finish_unknown_naive_or_expired_lease(claimed, lease):
    store, _source_id, queue, job = claimed
    _job_update(store, job["id"], lease_until=lease)
    assert queue.heartbeat(job["id"], "worker", attempt=1) is False
    assert queue.finish(job["id"], "worker", attempt=1, result_snapshot_id="invalid") is False


@pytest.mark.parametrize("attempt", [None, True, 0, -1, "1"])
def test_worker_mutations_refuse_unknown_or_invalid_generation(claimed, attempt):
    _store, _source_id, queue, job = claimed
    with pytest.raises(KnowledgeValidationError):
        queue.heartbeat(job["id"], "worker", attempt=attempt)
    with pytest.raises(KnowledgeValidationError):
        queue.finish(job["id"], "worker", attempt=attempt)
