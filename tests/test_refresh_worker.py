"""A stuck connector cannot hold the refresh worker indefinitely."""

from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import select, update

from knowledge_layer import worker
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.refresh_fence import (
    assert_refresh_fence,
    record_refresh_snapshot,
    refresh_fence,
)
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.store import KnowledgeStore


def test_worker_refuses_missing_connector_allowlist(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_ENABLED_KINDS", raising=False)
    with pytest.raises(RuntimeError, match="connector allowlist"):
        worker.run(once=True)


def test_worker_terminates_a_timed_out_child(monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def slow_child(*_args, **_kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(10)"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        children.append(child)
        return child

    class Queue:
        finished = None

        def heartbeat(self, *_args, **_kwargs):
            return True

        def finish(self, *_args, **kwargs):
            self.finished = kwargs
            return True

    queue = Queue()
    monkeypatch.setattr(worker.subprocess, "Popen", slow_child)
    worker._run_claimed(None, queue, {"id": "job_timeout", "attempt": 1}, "worker-test",
                        timeout_seconds=1, stopping=[False])
    assert children[0].poll() is not None
    assert queue.finished["error_code"] == "timeout"


@pytest.mark.parametrize("blocked", ["paused", "revoked", "disabled", "policy"])
def test_scheduled_child_rechecks_permissions_before_source_read(tmp_path, monkeypatch, blocked):
    from datetime import UTC, datetime

    from knowledge_layer.source_policy import SourcePolicy

    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'worker.db'}")
    source_id = store.create_connection("demo", kind="local_files", name="Worker", config={})["id"]
    schedules = RefreshSchedules(store)
    schedule = schedules.set("demo", source_id, {"cadence": "hourly", "timezone": "UTC", "minute": 0},
                             now=datetime(2026, 10, 3, 12, tzinfo=UTC))
    schedules.dispatch_due({"local_files"}, now=datetime(2026, 10, 3, 13, tzinfo=UTC))
    job = RefreshQueue(store).claim("worker-test")
    if blocked == "paused":
        schedules.set("demo", source_id, schedule["config"], paused=True)
    elif blocked == "revoked":
        with store.engine.begin() as conn:
            conn.execute(update(store.refresh_schedules).values(owner_subject="missing-owner"))
    elif blocked == "policy":
        store.source_policy = SourcePolicy.from_json('{"version":1,"sources":[]}')
    reads = []
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files" if blocked != "disabled" else "sqlite")
    monkeypatch.setattr(store, "refresh_connection", lambda *_args: reads.append(True))
    assert worker._execute(job["id"], "worker-test", job["attempt"]) == 2
    assert reads == []


def test_manual_child_is_not_revoked_by_paused_schedule(tmp_path, monkeypatch):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'manual.db'}")
    source_id = store.create_connection("demo", kind="local_files", name="Manual", config={})["id"]
    snapshot = store.save_snapshot("demo", source_id, {"snapshot_id": "manual-ready", "status": "ready"})
    RefreshSchedules(store).set("demo", source_id,
                                {"cadence": "hourly", "timezone": "UTC", "minute": 0}, paused=True)
    job = RefreshQueue(store).enqueue("demo", source_id)
    claimed = RefreshQueue(store).claim("worker-test")
    reads = []

    def refresh(*_args):
        reads.append(True)
        return {"connection": {"status": "ready"}, "snapshot": snapshot}

    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files")
    monkeypatch.setattr(store, "refresh_connection", refresh)
    assert worker._execute(job["id"], "worker-test", claimed["attempt"]) == 0
    assert reads == [True]


def test_worker_once_dispatches_due_before_claim(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'once.db'}")
    source_id = store.create_connection("demo", kind="local_files", name="Once", config={})["id"]
    RefreshSchedules(store).set("demo", source_id,
                                {"cadence": "hourly", "timezone": "UTC", "minute": 0},
                                now=datetime(2000, 1, 1, tzinfo=UTC))
    jobs = []
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files")
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(worker, "_run_claimed", lambda _store, _queue, job, *_args, **_kwargs: jobs.append(job))
    assert worker.run(once=True) == 0
    assert len(jobs) == 1 and jobs[0]["schedule_id"] is not None


@pytest.mark.parametrize("lost", ["attempt", "lease", "cancel"])
def test_child_refuses_stale_claim_before_source_read(tmp_path, monkeypatch, lost):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'stale-child.db'}")
    source = store.create_connection("demo", kind="local_files", name="Stale", config={})
    queue = RefreshQueue(store)
    queue.enqueue("demo", source["id"])
    job = queue.claim("worker")
    values = {"attempt": 2} if lost == "attempt" else {"lease_until": "2000-01-01T00:00:00Z"} if lost == "lease" else {"cancel_requested": True}
    with store.engine.begin() as conn:
        conn.execute(update(store.refresh_jobs).values(**values))
    reads = []
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files")
    monkeypatch.setattr(store, "refresh_connection", lambda *_args: reads.append(True))
    assert worker._execute(job["id"], "worker", job["attempt"]) == 2
    assert reads == []


def test_child_installs_fence_and_lost_claim_returns_cancel_code(tmp_path, monkeypatch):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'child-fence.db'}")
    source = store.create_connection("demo", kind="local_files", name="Child", config={})
    queue = RefreshQueue(store)
    queue.enqueue("demo", source["id"])
    job = queue.claim("worker")
    reads = []

    def refresh(tenant, source_id):
        with store.engine.begin() as conn:
            assert_refresh_fence(store, conn, tenant, source_id)
        reads.append(True)
        with store.engine.begin() as conn:
            conn.execute(update(store.refresh_jobs).values(attempt=job["attempt"] + 1))
        with store.engine.begin() as conn:
            assert_refresh_fence(store, conn, tenant, source_id)

    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files")
    monkeypatch.setattr(store, "refresh_connection", refresh)
    assert worker._execute(job["id"], "worker", job["attempt"]) == 2
    assert reads == [True]


def test_parent_passes_attempt_to_child_and_finish(tmp_path, monkeypatch):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'parent.db'}")
    source = store.create_connection("demo", kind="local_files", name="Parent", config={})
    store.save_snapshot("demo", source["id"], {"snapshot_id": "ready", "status": "ready"})
    queue = RefreshQueue(store)
    queue.enqueue("demo", source["id"])
    job = queue.claim("worker")
    with refresh_fence(job["id"], "worker", job["attempt"]), store.engine.begin() as conn:
        record_refresh_snapshot(store, conn, "demo", source["id"], "ready")
    # A later synchronous review/refresh must not be attributed to this child.
    store.save_snapshot("demo", source["id"], {"snapshot_id": "unrelated-latest", "status": "ready"})
    commands = []

    class CompleteChild:
        returncode = 0

        def poll(self):
            return self.returncode

    def popen(command, **_kwargs):
        commands.append(command)
        return CompleteChild()

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    worker._run_claimed(store, queue, job, "worker", timeout_seconds=30, stopping=[False])
    assert commands[0][-3:] == [job["id"], "worker", str(job["attempt"])]
    with store.engine.connect() as conn:
        row = conn.execute(select(store.refresh_jobs)).mappings().one()
    assert row["status"] == "succeeded" and row["result_snapshot_id"] == "ready"
    store.close()


@pytest.mark.parametrize("failed", [False, True])
def test_child_records_unchanged_snapshot_only_when_source_refresh_succeeded(tmp_path, monkeypatch, failed):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'unchanged.db'}")
    source = store.create_connection("demo", kind="local_files", name="Unchanged", config={})
    store.save_snapshot("demo", source["id"], {"snapshot_id": "retained", "status": "ready"})
    queue = RefreshQueue(store)
    queue.enqueue("demo", source["id"])
    job = queue.claim("worker")
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "local_files")
    monkeypatch.setattr(store, "refresh_connection", lambda *_args: {
        "connection": {"status": "error" if failed else "discovery_complete"},
        "snapshot": {"id": "retained"},
    })
    assert worker._execute(job["id"], "worker", job["attempt"]) == (1 if failed else 0)
    with store.engine.connect() as conn:
        result = conn.execute(select(store.refresh_jobs.c.result_snapshot_id)).scalar_one()
    assert result == (None if failed else "retained")
    store.close()


def test_execute_cli_refuses_missing_generation_before_store_access(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["knowledge_layer.worker", "execute", "job", "worker"])
    monkeypatch.setattr(worker, "_store", lambda: pytest.fail("store must not open"))
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2


def test_parent_heartbeat_carries_attempt_and_stops_child_on_lost_generation(monkeypatch):
    calls = []

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, **_kwargs):
            return self.returncode

    class Queue:
        def heartbeat(self, job_id, worker_id, *, attempt):
            calls.append(("heartbeat", job_id, worker_id, attempt))
            return False

        def finish(self, job_id, worker_id, **kwargs):
            calls.append(("finish", job_id, worker_id, kwargs))
            return False

    child = Child()
    clock = iter([0, 6, 6])
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    worker._run_claimed(None, Queue(), {"id": "job", "attempt": 7}, "worker", timeout_seconds=30, stopping=[False])
    assert child.returncode == -15
    assert calls[0] == ("heartbeat", "job", "worker", 7)
    assert calls[1][3]["attempt"] == 7 and calls[1][3]["cancelled"] is True
