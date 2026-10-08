"""Typed job effects on isolated registry metadata, never connected source IO.

SQLite proves transaction effects and generation predicates here. PostgreSQL
row-lock and migration/recovery execution remain independently qualified.
"""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, func, inspect, select, update

from knowledge_layer import worker
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_refresh import adapter_profile
from knowledge_layer.jobs import MAX_RETIRED_INVENTORIES, RefreshQueue
from knowledge_layer.migrations.versions import v0014_inventory_results
from knowledge_layer.refresh_fence import (
    RefreshFenceLost,
    record_refresh_inventory,
    record_refresh_snapshot,
    refresh_fence,
)
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeStore,
    KnowledgeValidationError,
)

CONFIG = {"secret_ref": "KNOWLEDGE_SOURCE_RESULT_FIXTURE", "scope": "finance",  # pragma: allowlist secret - public fixture operator reference
          "discovery_mode": "inventory", "retain_inventory_payloads": True}
LIMITS = {"max_pages": 100, "max_entities": 1000, "max_evidence": 1000,
          "max_total_bytes": 1024 * 1024}
OWNER = Principal("owner", frozenset({"finance"}), frozenset({"knowledge.sources.manage"}))


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_SOURCE_RESULT_FIXTURE", json.dumps({
        "endpoint": "https://adapter.invalid/discover", "provider": "fixture",
        "allowed_scopes": ["finance"], "protocol_version": "1.3",
        "revision": "fixed-metadata-fixture", "source_identity": "fixture.company.finance",
    }))
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'results.db'}")
    source = store.create_connection("demo", kind="external_adapter", name="Finance", config=CONFIG)
    queue = RefreshQueue(store)
    queue.enqueue("demo", source["id"])
    job = queue.claim("worker")
    yield store, source["id"], queue, job, InventoryStore(store)
    store.close()


def claim_for(job):
    return None if job is None else {"job_id": job["id"], "worker_id": job["worker_id"], "attempt": job["attempt"]}


def begin(inventory, source_id, job=None, **kwargs):
    policy = {"id": "external-adapter.inventory", "revision": adapter_profile(CONFIG)[1]}
    claim = claim_for(job)
    return inventory.begin("demo", source_id, scope={"namespace": "finance", "selectors": ["finance.*"]},
                           policy=policy, watermark={"kind": "unknown", "value": None},
                           limits=LIMITS, claim=claim, **kwargs)


def stage(inventory, source_id, job=None, **kwargs):
    run = begin(inventory, source_id, job, **kwargs)
    return inventory.append("demo", source_id, run["id"], page_index=0, cursor=None, next_cursor=None,
        entities=[{"id": "ledger", "kind": "asset", "payload": {"name": "Ledger", "clock": None},
                   "evidence_ids": ["schema"]}],
        evidence=[{"id": "schema", "kind": "schema", "payload": {"revision": None}}],
        scope_digest=run["scope_digest"], policy_digest=run["policy_digest"],
        watermark_digest=run["watermark_digest"], claim=claim_for(job))


def complete(inventory, source_id, job=None, *, source_refresh=True):
    run = stage(inventory, source_id, job)
    return inventory.commit("demo", source_id, run["id"], claim=claim_for(job), source_refresh=source_refresh,
                            completion={"listing_complete": True, "pages": run["page_count"],
                                        "entities": run["entity_count"], "evidence": run["evidence_count"],
                                        "manifest_digest": run["manifest_digest"]})


def row(store, table, identifier):
    with store.engine.connect() as conn:
        value = conn.execute(select(table).where(table.c.id == identifier)).mappings().one()
    return dict(value)


def mutate(store, table, identifier, **values):
    with store.engine.begin() as conn:
        conn.execute(update(table).where(table.c.id == identifier).values(**values))


def state(store, source_id):
    """Include heads, source clocks, legacy registry state, and full audit effects."""
    with store.engine.connect() as conn:
        source = dict(conn.execute(select(store.connections).where(store.connections.c.id == source_id)).mappings().one())
        heads = [dict(value) for value in conn.execute(select(store.inventory_heads)).mappings()]
        snapshots = conn.execute(select(func.count()).select_from(store.snapshots)).scalar_one()
        audits = [dict(value) for value in conn.execute(select(store.audit_events).order_by(store.audit_events.c.id)).mappings()]
    return source, heads, snapshots, audits


def test_commit_atomically_records_one_current_inventory_and_separate_clocks(registry):
    store, source_id, queue, job, inventory = registry
    with refresh_fence(job["id"], "worker", 1):
        run = complete(inventory, source_id, job)
    recorded = queue.worker_job(job["id"], "worker", attempt=1)
    assert recorded["result_inventory_id"] == run["id"] and recorded["result_snapshot_id"] is None
    assert queue.finish(job["id"], "worker", attempt=1, result_inventory_id=run["id"])
    finished = queue.get("demo", job["id"])
    assert finished["status"] == "succeeded" and finished["result_inventory_id"] == run["id"]
    source, heads, snapshots, _ = state(store, source_id)
    assert source["status"] == "inventory_complete" and source["last_error"] is None
    assert source["last_refreshed_at"] is None and source["last_complete_refresh_at"] is None
    assert snapshots == 0 and heads[0]["last_complete_scope_at"] == run["completed_at"]


@pytest.mark.parametrize("invalid", ["missing", "unrecorded", "staging", "foreign", "old-head", "wrong-claim", "bool-claim"])
def test_finish_refuses_unattributed_noncurrent_or_malformed_inventory_without_writes(registry, invalid):
    store, source_id, queue, job, inventory = registry
    with refresh_fence(job["id"], "worker", 1):
        run = complete(inventory, source_id, job)
    result_id = run["id"]
    if invalid == "missing":
        result_id = None
    elif invalid == "unrecorded":
        mutate(store, store.refresh_jobs, job["id"], result_inventory_id=None)
    elif invalid == "staging":
        mutate(store, store.inventory_runs, run["id"], status="staging")
    elif invalid == "foreign":
        other = store.create_connection("demo", kind="local_files", name="Other", config={})
        mutate(store, store.inventory_runs, run["id"], connection_id=other["id"])
    elif invalid == "old-head":
        # Another actual completed scoped listing supersedes this generation.
        complete(inventory, source_id, source_refresh=False)
    elif invalid in {"wrong-claim", "bool-claim"}:
        claim = deepcopy(claim_for(job))
        claim["worker_id" if invalid == "wrong-claim" else "attempt"] = "other" if invalid == "wrong-claim" else True
        mutate(store, store.inventory_runs, run["id"], claim=claim)
    before = state(store, source_id), row(store, store.refresh_jobs, job["id"])
    with pytest.raises(KnowledgeConflict):
        queue.finish(job["id"], "worker", attempt=1, result_inventory_id=result_id)
    assert (state(store, source_id), row(store, store.refresh_jobs, job["id"])) == before


def test_both_typed_results_refuse_even_when_reporting_failure(registry):
    store, _source_id, queue, job, _inventory = registry
    before = row(store, store.refresh_jobs, job["id"])
    with pytest.raises(KnowledgeValidationError):
        queue.finish(job["id"], "worker", attempt=1, result_snapshot_id="legacy",
                     result_inventory_id="inventory", error_code="failure")
    assert row(store, store.refresh_jobs, job["id"]) == before


def test_record_without_fence_is_noop_and_cross_source_fence_refuses(registry):
    store, source_id, _queue, job, _inventory = registry
    with store.engine.begin() as conn:
        record_refresh_inventory(store, conn, "demo", source_id, "not-invented")
    assert row(store, store.refresh_jobs, job["id"])["result_inventory_id"] is None
    with refresh_fence(job["id"], "worker", 1), store.engine.begin() as conn, pytest.raises(RefreshFenceLost):
        record_refresh_inventory(store, conn, "another", source_id, "not-invented")


@pytest.mark.parametrize("prior", ["snapshot", "inventory"])
def test_recording_another_typed_result_refuses(registry, prior):
    store, source_id, _queue, job, inventory = registry
    with refresh_fence(job["id"], "worker", 1):
        run = complete(inventory, source_id, job)
    if prior == "snapshot":
        mutate(store, store.refresh_jobs, job["id"], result_inventory_id=None, result_snapshot_id="prior")
        with refresh_fence(job["id"], "worker", 1), store.engine.begin() as conn, pytest.raises(RefreshFenceLost):
            record_refresh_inventory(store, conn, "demo", source_id, run["id"])
    else:
        with refresh_fence(job["id"], "worker", 1), store.engine.begin() as conn, pytest.raises(RefreshFenceLost):
            record_refresh_snapshot(store, conn, "demo", source_id, "legacy")


def test_late_expiry_rolls_back_commit_head_audit_source_and_result(registry, monkeypatch):
    store, source_id, queue, job, inventory = registry
    run = stage(inventory, source_id, job)
    before = state(store, source_id), row(store, store.inventory_runs, run["id"])
    from knowledge_layer import refresh_fence as fencing

    check = fencing.assert_refresh_fence
    calls = []

    def late(store, conn, tenant, source):
        calls.append(True)
        if conn.execute(select(store.refresh_jobs.c.result_inventory_id)).scalar_one() is not None:
            raise RefreshFenceLost("Lease expired at final attribution")
        return check(store, conn, tenant, source)

    monkeypatch.setattr(fencing, "assert_refresh_fence", late)
    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        inventory.commit("demo", source_id, run["id"], claim=claim_for(job), source_refresh=True,
            completion={"listing_complete": True, "pages": 1, "entities": 1, "evidence": 1,
                        "manifest_digest": run["manifest_digest"]})
    assert calls and (state(store, source_id), row(store, store.inventory_runs, run["id"])) == before
    assert queue.worker_job(job["id"], "worker", attempt=1)["result_inventory_id"] is None


@pytest.mark.parametrize("outcome", ["retry", "failed", "cancelled", "takeover", "exhausted"])
def test_retirement_aborts_only_exact_attempt_staging_and_preserves_completed_head(registry, outcome):
    store, source_id, queue, job, inventory = registry
    completed = complete(inventory, source_id, source_refresh=False)
    staged = stage(inventory, source_id, job)
    manual = stage(inventory, source_id)
    source_before, heads_before, snapshots_before, _ = state(store, source_id)
    if outcome in {"failed", "exhausted"}:
        mutate(store, store.refresh_jobs, job["id"], max_attempts=1)
    if outcome == "cancelled":
        queue.cancel("demo", job["id"])
        assert queue.finish(job["id"], "worker", attempt=1, cancelled=True)
    elif outcome in {"takeover", "exhausted"}:
        mutate(store, store.refresh_jobs, job["id"], lease_until="2000-01-01T00:00:00Z")
        # Operator policy withdrawal must not prevent local failed-attempt repair.
        store.source_policy = SourcePolicy.from_json('{"version":1,"sources":[]}')
        reclaimed = queue.claim("replacement")
        assert (reclaimed is None) == (outcome == "exhausted")
        if reclaimed:
            assert reclaimed["attempt"] == 2 and reclaimed["result_inventory_id"] is None
    else:
        assert queue.finish(job["id"], "worker", attempt=1, error_code="page_failed")
    assert row(store, store.inventory_runs, staged["id"])["status"] == "aborted"
    assert row(store, store.inventory_runs, manual["id"])["status"] == "staging"
    assert row(store, store.inventory_runs, completed["id"])["status"] == "committed"
    source_after, heads_after, snapshots_after, audits = state(store, source_id)
    assert heads_after == heads_before and snapshots_after == snapshots_before
    assert all(source_after[key] == source_before[key] for key in (
        "last_refresh_attempt_at", "last_complete_refresh_at", "last_refreshed_at"))
    aborted = [event for event in audits if event["event_type"] == "inventory.abort"]
    assert len(aborted) == 1 and aborted[0]["details"] == {
        "inventory_id": staged["id"], "job_id": job["id"], "attempt": 1, "reason": "refresh_attempt_retired"}


def test_cancelled_expired_attempt_reconciles_before_terminal_claim(registry):
    store, source_id, queue, job, inventory = registry
    staged = stage(inventory, source_id, job)
    queue.cancel("demo", job["id"])
    mutate(store, store.refresh_jobs, job["id"], lease_until="2000-01-01T00:00:00Z")
    assert queue.claim("next") is None
    assert row(store, store.refresh_jobs, job["id"])["status"] == "cancelled"
    assert row(store, store.inventory_runs, staged["id"])["status"] == "aborted"


@pytest.mark.parametrize("guard", ["overflow", "head-reference", "previous-reference", "bool-claim", "audit"])
def test_uncertain_retirement_rolls_back_generation_and_all_staging(registry, monkeypatch, guard):
    store, source_id, queue, job, inventory = registry
    run = stage(inventory, source_id, job)
    if guard == "overflow":
        for _ in range(MAX_RETIRED_INVENTORIES):
            begin(inventory, source_id, job)
    elif guard == "head-reference":
        # Simulate a corrupt current pointer to a staging row; do not hide it.
        with store.engine.begin() as conn:
            conn.execute(store.inventory_heads.insert().values(tenant_id="demo", connection_id=source_id,
                scope_digest=run["scope_digest"], policy_digest=run["policy_digest"], inventory_id=run["id"],
                manifest_digest=run["manifest_digest"], generation=1, last_complete_scope_at=run["created_at"]))
    elif guard == "previous-reference":
        other = begin(inventory, source_id)
        mutate(store, store.inventory_runs, other["id"], previous_inventory_id=run["id"])
    elif guard == "bool-claim":
        mutate(store, store.inventory_runs, run["id"], claim={**claim_for(job), "attempt": True})
    else:
        original = store._audit

        def fail_audit(conn, tenant, source, event, *args):
            if event == "inventory.abort":
                raise RuntimeError("Injected registry failure")
            return original(conn, tenant, source, event, *args)

        monkeypatch.setattr(store, "_audit", fail_audit)
    before = state(store, source_id), row(store, store.refresh_jobs, job["id"])
    with pytest.raises((KnowledgeConflict, RuntimeError)):
        queue.finish(job["id"], "worker", attempt=1, error_code="page_failed")
    assert (state(store, source_id), row(store, store.refresh_jobs, job["id"])) == before
    assert row(store, store.inventory_runs, run["id"])["status"] == "staging"


def test_takeover_clears_both_result_types_and_prevents_old_child_mutations(registry):
    store, source_id, queue, job, inventory = registry
    run = stage(inventory, source_id, job)
    mutate(store, store.refresh_jobs, job["id"], result_inventory_id="previous-attribution",
           result_snapshot_id="previous-snapshot", lease_until="2000-01-01T00:00:00Z")
    current = queue.claim("worker")
    assert current["attempt"] == 2 and current["result_inventory_id"] is None and current["result_snapshot_id"] is None
    with refresh_fence(job["id"], "worker", 1), pytest.raises(RefreshFenceLost):
        begin(inventory, source_id, job)
    assert not queue.finish(job["id"], "worker", attempt=1, result_inventory_id=run["id"])
    assert not queue.heartbeat(job["id"], "worker", attempt=1)


@pytest.mark.parametrize("authorization", ["valid", "group-only-approval", "group-only-grant", "revoked-direct", "operator"])
def test_inventory_enqueue_requires_explicit_subject_and_direct_manage_before_reuse(registry, authorization):
    store, source_id, queue, job, _inventory = registry
    store.grant_source_access("demo", source_id, principal_type="user", principal_id="owner", role="manage")
    store.grant_source_access("demo", source_id, principal_type="group", principal_id="finance", role="manage")
    selector = {key: value for key, value in CONFIG.items() if key != "inventory_limits"}
    store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
        "tenant_id": "demo", "kind": "external_adapter", "selector": selector,
        "allowed_subjects": [] if authorization == "group-only-approval" else ["owner"],
        "allowed_groups": ["finance"],
    }]}))
    store.access_enforced = True
    if authorization in {"group-only-grant", "revoked-direct"}:
        with store.engine.begin() as conn:
            conn.execute(store.source_grants.delete().where(store.source_grants.c.principal_type == "user"))
    actor = Principal("operator", frozenset({"finance"}), OWNER.scopes) if authorization == "operator" else OWNER
    before = state(store, source_id)
    with principal_scope(actor):
        if authorization == "valid":
            assert queue.enqueue("demo", source_id)["id"] == job["id"]
        else:
            with pytest.raises(KnowledgeAccessDenied):
                queue.enqueue("demo", source_id)
    assert state(store, source_id) == before


@pytest.mark.parametrize("result", [None, {}, {"inventory": {}}, {"snapshot": {"id": "legacy"}},
                                    {"snapshot": {"id": "legacy"}, "inventory": {"id": "native"}},
                                    {"inventory": {"id": "invented"}}, {"inventory": {"id": "x" * 65}}])
def test_child_refuses_missing_ambiguous_or_unrecorded_results(registry, monkeypatch, result):
    store, _source_id, queue, job, _inventory = registry
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "external_adapter")
    monkeypatch.setattr(store, "refresh_connection", lambda *_: None if result is None else {
        "connection": {"status": "inventory_complete"}, **result})
    assert worker._execute(job["id"], "worker", 1) == 1
    assert queue.worker_job(job["id"], "worker", attempt=1)["result_inventory_id"] is None


def test_child_and_parent_consume_the_actual_recorded_inventory(registry, monkeypatch):
    store, source_id, queue, job, inventory = registry
    monkeypatch.setattr(worker, "_store", lambda: store)
    monkeypatch.setenv("KNOWLEDGE_ENABLED_KINDS", "external_adapter")

    def refresh(*_):
        run = complete(inventory, source_id, job)
        return {"connection": {"status": "inventory_complete"}, "snapshot": None, "inventory": run}

    monkeypatch.setattr(store, "refresh_connection", refresh)
    assert worker._execute(job["id"], "worker", 1) == 0
    actual = queue.worker_job(job["id"], "worker", attempt=1)["result_inventory_id"]

    class Child:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    worker._run_claimed(store, queue, job, "worker", timeout_seconds=30, stopping=[False])
    finished = queue.get("demo", job["id"])
    assert finished["status"] == "succeeded" and finished["result_inventory_id"] == actual
    assert finished["result_snapshot_id"] is None


def test_parent_stale_inventory_retries_without_claiming_newer_head(registry, monkeypatch):
    store, source_id, queue, job, inventory = registry
    with refresh_fence(job["id"], "worker", 1):
        older = complete(inventory, source_id, job)
    newer = complete(inventory, source_id, source_refresh=False)
    before = state(store, source_id)

    class Child:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    worker._run_claimed(store, queue, job, "worker", timeout_seconds=30, stopping=[False])
    finished = queue.get("demo", job["id"])
    assert finished["status"] == "retry_wait" and finished["error_code"] == "inventory_result_stale"
    assert finished["result_snapshot_id"] is None and finished["result_inventory_id"] is None
    after = state(store, source_id)
    assert after[:3] == before[:3]
    assert after[1][0]["inventory_id"] == newer["id"] != older["id"]


@pytest.mark.parametrize("records,code", [({}, "missing_result"),
                                         ({"result_inventory_id": "native", "result_snapshot_id": "legacy"}, "ambiguous_result")])
def test_parent_missing_or_ambiguous_attribution_retries_without_inventing_id(registry, monkeypatch, records, code):
    store, _source_id, queue, job, _inventory = registry
    if records:
        mutate(store, store.refresh_jobs, job["id"], **records)

    class Child:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    worker._run_claimed(store, queue, job, "worker", timeout_seconds=30, stopping=[False])
    finished = queue.get("demo", job["id"])
    assert finished["status"] == "retry_wait" and finished["error_code"] == code
    assert finished["result_snapshot_id"] is None and finished["result_inventory_id"] is None


def test_v14_adds_nullable_typed_result_without_rewriting_existing_job():
    engine = create_engine("sqlite:///:memory:")
    prior = Table("knowledge_refresh_jobs", MetaData(), Column("id", String(64), primary_key=True),
                  Column("result_snapshot_id", String(128)))
    prior.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(prior.insert().values(id="prior-job", result_snapshot_id="prior-snapshot"))
        with Operations.context(MigrationContext.configure(conn)):
            v0014_inventory_results.upgrade()
        current = Table(prior.name, MetaData(), autoload_with=conn)
        before = dict(conn.execute(select(current)).mappings().one())
        assert before == {"id": "prior-job", "result_snapshot_id": "prior-snapshot", "result_inventory_id": None}
        conn.execute(current.update().values(result_inventory_id="actual-inventory"))
        assert conn.execute(select(current.c.result_inventory_id)).scalar_one() == "actual-inventory"
    assert len(inspect(engine).get_table_names()) == 1
    assert v0014_inventory_results.down_revision == "0013_autonomy"
    with pytest.raises(RuntimeError, match="forward-only"):
        v0014_inventory_results.downgrade()
    engine.dispose()


def test_recovery_v14_preserves_all_prior_tables_and_historical_v13_support():
    path = Path(__file__).parents[1] / "scripts" / "drill_postgres_recovery.py"
    spec = importlib.util.spec_from_file_location("inventory_recovery", path)
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    assert recovery.TABLES_BY_REVISION["0014_inventory_results"] == recovery.TABLES_BY_REVISION["0013_autonomy"]
    assert len(recovery.TABLES_BY_REVISION["0014_inventory_results"]) == 23
