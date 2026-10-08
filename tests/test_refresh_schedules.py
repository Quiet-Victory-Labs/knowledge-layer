"""Calendar, authorization and atomic-dispatch acceptance for refresh schedules."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.schedules import RefreshSchedules, next_due, validate_schedule_config
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)

BASE = {"cadence": "hourly", "timezone": "UTC", "minute": 0}
START = datetime(2026, 10, 3, 12, tzinfo=UTC)


def clock(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(("config", "now", "expected"), [
    (BASE, "2026-10-03T12:00:00Z", "2026-10-03T13:00:00Z"),
    ({**BASE, "timezone": "Asia/Kathmandu", "minute": 10},
     "2026-10-03T12:00:00Z", "2026-10-03T12:25:00Z"),
    ({**BASE, "timezone": "America/New_York", "minute": 30},
     "2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"),
    ({**BASE, "timezone": "Antarctica/Troll"},
     "2026-10-25T00:30:00Z", "2026-10-25T01:00:00Z"),
    ({**BASE, "timezone": "America/New_York", "minute": 30},
     "2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z"),
    ({"cadence": "daily", "timezone": "America/New_York", "hour": 1, "minute": 30},
     "2026-11-01T05:30:00Z", "2026-11-02T06:30:00Z"),
    ({"cadence": "daily", "timezone": "America/New_York", "hour": 2, "minute": 30},
     "2026-03-08T05:00:00Z", "2026-03-09T06:30:00Z"),
    ({"cadence": "weekly", "timezone": "America/New_York", "weekday": 6, "hour": 2, "minute": 30},
     "2026-03-08T05:00:00Z", "2026-03-15T06:30:00Z"),
    ({"cadence": "weekly", "timezone": "UTC", "weekday": 0, "hour": 4, "minute": 10},
     "2026-10-03T12:00:00Z", "2026-10-05T04:10:00Z"),
    ({"cadence": "monthly", "timezone": "UTC", "day": 31, "hour": 0, "minute": 0},
     "2026-01-31T00:00:00Z", "2026-02-28T00:00:00Z"),
    ({"cadence": "monthly", "timezone": "UTC", "day": 31, "hour": 0, "minute": 0},
     "2028-01-31T00:00:00Z", "2028-02-29T00:00:00Z"),
])
def test_calendar_occurrences(config, now, expected):
    assert next_due(config, clock(now)) == clock(expected)


@pytest.mark.parametrize("config", [
    None, [], {}, {**BASE, "cadence": "minutely"}, {**BASE, "timezone": "No/SuchZone"},
    {**BASE, "timezone": "/etc/passwd"}, {**BASE, "timezone": "localtime"},
    {**BASE, "timezone": "posix/UTC"}, {**BASE, "minute": True}, {**BASE, "minute": 60},
    {**BASE, "minute": -1}, {**BASE, "minute": 1.0}, {**BASE, "hour": 3},
    {**BASE, "unknown": 1}, {**BASE, "cadence": "daily"},
    {**BASE, "cadence": "daily", "hour": False},
    {**BASE, "cadence": "weekly", "hour": 1, "weekday": 7},
    {**BASE, "cadence": "monthly", "hour": 1, "day": 0},
    {**BASE, "freshness_max_age_seconds": False}, {**BASE, "freshness_max_age_seconds": 59},
    {**BASE, "freshness_max_age_seconds": 2678401},
])
def test_invalid_schedule_refuses(config):
    with pytest.raises(KnowledgeValidationError):
        validate_schedule_config(config)


def test_naive_clock_refuses():
    with pytest.raises(KnowledgeValidationError, match="aware"):
        next_due(BASE, clock("2026-10-03T12:00:00"))


@pytest.fixture
def store(tmp_path):
    value = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'schedules.db'}")
    yield value
    value.close()


def source(store, name="Schedule source"):
    return store.create_connection("demo", kind="local_files", name=name, config={})["id"]


def test_pause_resume_unique_and_tenant_scope(store):
    source_id = source(store)
    schedules = RefreshSchedules(store)
    assert schedules.get("demo", source_id) is None
    first = schedules.set("demo", source_id, BASE, now=START)
    paused = schedules.set("demo", source_id, BASE, paused=True, now=START)
    assert first["id"] == paused["id"] and paused["next_run_at"] is None
    assert schedules.dispatch_due({"local_files"}, now=clock("2027-01-01T00:00:00Z")) == []
    resumed = schedules.set("demo", source_id, BASE, now=clock("2027-01-01T00:30:00Z"))
    assert resumed["id"] == first["id"]
    assert resumed["next_run_at"] == "2027-01-01T01:00:00.000000Z"
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.refresh_schedules)).scalar_one() == 1
    with pytest.raises(KnowledgeNotFound):
        schedules.get("other", source_id)
    with pytest.raises(KnowledgeValidationError, match="boolean"):
        schedules.set("demo", source_id, BASE, paused=1)


def test_missed_occurrences_coalesce_without_relabeling_manual_job(store):
    source_id = source(store)
    schedules = RefreshSchedules(store)
    schedules.set("demo", source_id, BASE, now=START)
    manual = RefreshQueue(store).enqueue("demo", source_id)
    results = schedules.dispatch_due({"local_files"}, now=clock("2027-01-01T01:30:00Z"))
    assert results[0]["status"] == "coalesced" and results[0]["job_id"] == manual["id"]
    current = schedules.get("demo", source_id)
    assert current["last_enqueued_at"] is None and current["last_job_id"] == manual["id"]
    assert current["next_run_at"] == "2027-01-01T02:00:00.000000Z"
    assert RefreshQueue(store).get("demo", manual["id"])["schedule_id"] is None
    schedules.set("demo", source_id, BASE, paused=True)
    assert RefreshQueue(store).get("demo", manual["id"])["status"] == "queued"


def test_one_scheduled_job_and_no_unbounded_catchup(store):
    source_id = source(store)
    schedules = RefreshSchedules(store)
    schedule = schedules.set("demo", source_id, BASE, now=START)
    dispatch = schedules.dispatch_due({"local_files"}, now=clock("2027-01-01T01:30:00Z"))
    job = RefreshQueue(store).get("demo", dispatch[0]["job_id"])
    assert dispatch[0]["status"] == "enqueued" and job["schedule_id"] == schedule["id"]
    assert schedules.dispatch_due({"local_files"}, now=clock("2027-01-01T01:31:00Z")) == []
    assert schedules.get("demo", source_id)["last_enqueued_at"] == "2027-01-01T01:30:00.000000Z"
    schedules.set("demo", source_id, BASE, paused=True)
    assert RefreshQueue(store).get("demo", job["id"])["status"] == "cancelled"


def test_capacity_keeps_due_and_later_enqueues(store):
    queue = RefreshQueue(store)
    busy_id, due_id = source(store, "Busy"), source(store, "Due")
    busy = queue.enqueue("demo", busy_id)
    schedules = RefreshSchedules(store)
    before = schedules.set("demo", due_id, BASE, now=START)
    now = clock("2026-10-03T15:00:00Z")
    result = schedules.dispatch_due({"local_files"}, max_pending=1, now=now)
    assert result[0]["status"] == "capacity"
    current = schedules.get("demo", due_id)
    assert current["next_run_at"] == before["next_run_at"]
    assert current["last_error"] == "queue_capacity" and current["last_enqueued_at"] is None
    queue.cancel("demo", busy["id"])
    assert schedules.dispatch_due({"local_files"}, max_pending=1, now=now)[0]["status"] == "enqueued"


def test_saturated_due_prefix_does_not_starve_later_active_source_coalescing(store):
    schedules = RefreshSchedules(store)
    entries = []
    for index in range(22):
        source_id = source(store, f"Due source {index}")
        entry = schedules.set("demo", source_id, BASE, now=START)
        due = START + timedelta(minutes=index)
        with store.engine.begin() as conn:
            conn.execute(update(store.refresh_schedules).where(
                store.refresh_schedules.c.id == entry["id"],
            ).values(next_run_at=due.isoformat(timespec="microseconds").replace("+00:00", "Z")))
        entries.append(entry)
    active = RefreshQueue(store).enqueue("demo", entries[-1]["connection_id"])
    result = schedules.dispatch_due({"local_files"}, max_pending=1,
                                    now=clock("2026-10-03T15:00:00Z"))
    assert len(result) == 20
    assert result[0] == {"schedule_id": entries[-1]["id"], "job_id": active["id"], "status": "coalesced"}
    assert sum(item["status"] == "capacity" for item in result) == 19
    current = schedules.get("demo", entries[-1]["connection_id"])
    assert current["next_run_at"] == "2026-10-03T16:00:00.000000Z"
    assert RefreshQueue(store).get("demo", active["id"])["schedule_id"] is None
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.refresh_jobs)).scalar_one() == 1


def test_dispatch_limit_and_atomic_rollback(store, monkeypatch):
    schedules = RefreshSchedules(store)
    for index in range(3):
        schedules.set("demo", source(store, f"Source {index}"), BASE, now=START)
    now = clock("2026-10-03T15:00:00Z")
    assert len(schedules.dispatch_due({"local_files"}, limit=2, now=now)) == 2
    with store.engine.connect() as conn:
        due = dict(conn.execute(select(store.refresh_schedules).where(
            store.refresh_schedules.c.last_job_id.is_(None),
        )).mappings().one())

    def refuse_audit(*_args, **_kwargs):
        raise RuntimeError("Injected registry failure")

    monkeypatch.setattr(store, "_audit", refuse_audit)
    with pytest.raises(RuntimeError, match="registry failure"):
        schedules.dispatch_due({"local_files"}, now=now)
    with store.engine.connect() as conn:
        row = conn.execute(select(store.refresh_schedules).where(
            store.refresh_schedules.c.id == due["id"],
        )).mappings().one()
        assert row["next_run_at"] == due["next_run_at"] and row["last_job_id"] is None
        assert conn.execute(select(func.count()).select_from(store.refresh_jobs)).scalar_one() == 2


def test_unknown_and_complete_success_freshness_are_separate_from_error(store):
    source_id = source(store)
    schedules = RefreshSchedules(store)
    schedules.set("demo", source_id, {**BASE, "freshness_max_age_seconds": 3600}, now=START)
    assert schedules.get("demo", source_id, now=START)["freshness"]["state"] == "unknown"
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).where(store.connections.c.id == source_id).values(
            last_complete_refresh_at="2026-10-03T11:30:00Z", last_refresh_attempt_at="2026-10-03T11:55:00Z",
            status="error", last_error="Failed read",
        ))
    current = schedules.get("demo", source_id, now=START)
    assert current["freshness"]["state"] == "within_window" and current["refresh_error"] is True
    assert current["source_status"] == "error" and current["freshness"]["age_seconds"] == 1800
    assert schedules.get("demo", source_id, now=clock("2026-10-03T12:31:00Z"))["freshness"]["state"] == "overdue"


def managed(store):
    store.access_enforced = True
    store.source_admin_group = "stewards"
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "local_files", "selector": {"path": ".", "include": "**/*"},
        "allowed_subjects": ["owner", "operator", "group-owner"], "allowed_groups": ["stewards"],
    }]}))
    return Principal("owner", frozenset({"stewards"}))


def test_direct_owner_grant_revocation_blocks_dispatch_and_queued_read(store):
    owner = managed(store)
    schedules = RefreshSchedules(store)
    with principal_scope(owner):
        source_id = source(store)
        schedule = schedules.set("demo", source_id, BASE, now=START)
    store.access_enforced = False
    dispatch = schedules.dispatch_due({"local_files"}, now=clock("2026-10-03T13:00:00Z"))
    queue = RefreshQueue(store)
    job = queue.claim("worker")
    assert job["id"] == dispatch[0]["job_id"]
    store.revoke_source_access("demo", source_id, principal_type="user", principal_id="owner")
    assert schedules.job_allowed(job, enabled_kinds={"local_files"}) is False
    current = schedules.get("demo", source_id)
    assert current["paused"] and current["last_error"] == "owner_grant_revoked"
    assert current["id"] == schedule["id"]
    assert queue.worker_job(job["id"], "worker")["cancel_requested"] is True


def test_group_only_manage_and_reserved_owner_cannot_create_schedule(store):
    owner = managed(store)
    with principal_scope(owner):
        source_id = source(store)
    store.access_enforced = False
    store.grant_source_access("demo", source_id, principal_type="group", principal_id="stewards", role="manage")
    store.access_enforced = True
    with (principal_scope(Principal("group-owner", frozenset({"stewards"}))),
          pytest.raises(KnowledgeAccessDenied, match="direct user")):
        RefreshSchedules(store).set("demo", source_id, BASE)
    with (principal_scope(Principal("operator", frozenset({"stewards"}))),
          pytest.raises(KnowledgeAccessDenied, match="reserved")):
        RefreshSchedules(store).set("demo", source_id, BASE)
    with principal_scope(owner), pytest.raises(KnowledgeAccessDenied, match="operator process"):
        RefreshSchedules(store).dispatch_due({"local_files"})


@pytest.mark.parametrize(("enabled", "policy_denied", "expected"), [
    (set(), False, "connector_disabled"), ({"local_files"}, True, "source_not_approved"),
])
def test_disabled_connector_and_withdrawn_policy_pause(store, enabled, policy_denied, expected):
    source_id = source(store)
    schedules = RefreshSchedules(store)
    schedules.set("demo", source_id, BASE, now=START)
    if policy_denied:
        store.source_policy = SourcePolicy.from_json('{"version":1,"sources":[]}')
    results = schedules.dispatch_due(enabled, now=clock("2026-10-03T13:00:00Z"))
    assert results[0]["error_code"] == expected
    with store.engine.connect() as conn:
        row = conn.execute(select(store.refresh_schedules)).mappings().one()
        assert row["paused"] and row["next_run_at"] is None and row["last_error"] == expected
        assert conn.execute(select(func.count()).select_from(store.refresh_jobs)).scalar_one() == 0


def test_revocation_before_due_dispatch_creates_no_job(store):
    owner = managed(store)
    schedules = RefreshSchedules(store)
    with principal_scope(owner):
        source_id = source(store)
        schedules.set("demo", source_id, BASE, now=START)
    store.access_enforced = False
    store.revoke_source_access("demo", source_id, principal_type="user", principal_id="owner")
    result = schedules.dispatch_due({"local_files"}, now=clock("2026-10-03T13:00:00Z"))
    assert result[0]["status"] == "blocked" and result[0]["error_code"] == "owner_grant_revoked"
    with store.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(store.refresh_jobs)).scalar_one() == 0


@pytest.mark.parametrize("clock_value", [None, "2026-10-03T13:00:00Z", "2026-10-03T11:00:00", "bad"])
def test_missing_future_or_invalid_complete_clock_stays_unknown(store, clock_value):
    source_id = source(store)
    schedules = RefreshSchedules(store)
    schedules.set("demo", source_id, {**BASE, "freshness_max_age_seconds": 60}, now=START)
    with store.engine.begin() as conn:
        conn.execute(update(store.connections).where(store.connections.c.id == source_id).values(
            last_complete_refresh_at=clock_value,
        ))
    result = schedules.get("demo", source_id, now=START)
    assert result["freshness"]["state"] == "unknown" and result["freshness"]["age_seconds"] is None
