#!/usr/bin/env python3
"""Qualify paged refresh workers against disposable PostgreSQL and synthetic HTTP.

No customer credentials, provider calls, models, source SQL, or semantic
approval are involved. The only HTTP server is a nonce-owned loopback fixture,
started when this explicit drill is run and removed before a passed receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep

from drill_inventory import DrillFailed, _census, _cleanup_registry, _database_url, _write_report
from sqlalchemy import delete, func, inspect, select, text, update

from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_inventory import validate_inventory_request
from knowledge_layer.inventory import InventoryStore
from knowledge_layer.inventory_refresh import adapter_profile
from knowledge_layer.jobs import ACTIVE, RefreshQueue
from knowledge_layer.migrate import ensure_current_schema, run
from knowledge_layer.refresh_fence import refresh_fence
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeAccessDenied, KnowledgeNotFound, KnowledgeStore, utc_now

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {"max_pages": 4, "max_entities": 4, "max_evidence": 4, "max_total_bytes": 32768}
ENTITY = {"id": "fixture.asset", "kind": "asset", "evidence_ids": ["fixture.schema"],
          "payload": {"native_id": "fixture.asset", "name": "Synthetic Ledger", "native_version": None,
                      "fields": [{"id": "fixture.amount", "name": "amount", "native_type": "numeric",
                                  "nullable": None}], "description": "Synthetic contributor metadata only."}}
EVIDENCE = {"id": "fixture.schema", "kind": "schema_observation",
            "payload": {"native_id": "fixture.schema", "native_version": None,
                        "observed_at": None, "reference": "fixture/schema/ledger"}}


def _check(report, name, condition, **details):
    report["checks"].append({"name": name, "passed": bool(condition), **details})
    if not condition:
        raise DrillFailed(name)


@contextmanager
def _environment(values):
    before = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, allow_nan=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


class FixtureAdapter(ThreadingHTTPServer):
    """Fixed two-page generic contributor response, never an upstream client."""

    daemon_threads = True

    def __init__(self, token):
        self.token = token
        self.mode = "success"
        self.cancel_attempt = None
        self.calls = []
        self.guard = threading.Lock()
        super().__init__(("127.0.0.1", 0), FixtureHandler)

    def observations(self):
        with self.guard:
            return list(self.calls)

    def handle_error(self, _request, _client_address):
        return


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _send(self, status, value):
        encoded = json.dumps(value, allow_nan=False, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        self.connection.settimeout(5)
        if self.path != "/discover" or self.headers.get("Authorization") != "Bearer " + self.server.token:
            self._send(403, {"error": "fixture_denied"})
            return
        try:
            count = int(self.headers.get("Content-Length", "0"))
            if not 1 <= count <= 16384 or self.headers.get("Transfer-Encoding"):
                raise ValueError
            request = validate_inventory_request(json.loads(self.rfile.read(count)))
            if request["provider"] != "fixture" or request["scope"] != "finance":
                raise ValueError
        except (ConnectorError, ValueError, TypeError, OSError):
            self._send(400, {"error": "fixture_invalid_request"})
            return
        # Only index/scan provenance is retained; never tokens or request text.
        with self.server.guard:
            mode = self.server.mode
            self.server.calls.append({"page_index": request["page_index"], "scan_id": request["scan_id"]})
        index = request["page_index"]
        if (index not in {0, 1} or (index == 0 and request["cursor"] is not None)
                or (index == 1 and request["cursor"] != "fixture.page.2")):
            self._send(400, {"error": "fixture_invalid_cursor"})
            return
        if mode in {"fail_second", "cancel_second"} and index == 1:
            if mode == "cancel_second":
                if self.server.cancel_attempt is None:
                    self._send(500, {"error": "fixture_cancel_unavailable"})
                    return
                self.server.cancel_attempt()
            self._send(503, {"error": "synthetic_second_page_failure"})
            return
        excluded = {"inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}
        response = {key: value for key, value in request.items() if key not in excluded}
        response.update(next_cursor="fixture.page.2" if index == 0 else None,
                        listing_complete=index == 1, watermark={"kind": "unknown", "value": None},
                        entities=[deepcopy(ENTITY)] if index == 0 else [],
                        evidence=[deepcopy(EVIDENCE)] if index == 1 else [])
        self._send(200, response)


def _worker_environment(policy_file, reference, profile_file, nonce):
    environment = dict(os.environ)
    for key in ("KNOWLEDGE_SOURCE_POLICY", "KNOWLEDGE_SOURCE_POLICY_FILE", reference, reference + "_FILE"):
        environment.pop(key, None)
    environment.update({"KNOWLEDGE_AUTH_MODE": "oidc", "KNOWLEDGE_ENABLED_KINDS": "external_adapter",
                        "KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS": "1",
                        "KNOWLEDGE_SOURCE_POLICY_FILE": str(policy_file), reference + "_FILE": str(profile_file),
                        "PGAPPNAME": "kl_inventory_refresh_" + nonce,
                        # A controlled disposable-registry option, not caller SQL.
                        # Inventory writers must explicitly restore READ COMMITTED.
                        "PGOPTIONS": "-c default_transaction_isolation=repeatable\\ read"})
    return environment


def _launch_worker(environment, *, claim=None):
    arguments = (["--once", "--timeout-seconds", "60"] if claim is None else
                 ["execute", claim["id"], claim["worker_id"], str(claim["attempt"])])
    return subprocess.Popen([sys.executable, "-m", "knowledge_layer.worker", *arguments],
                            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def _complete_worker(child, *, expected_codes=(0,)):
    try:
        result = child.wait(timeout=90)
        if result not in expected_codes:
            raise DrillFailed("worker_process_failed")
        return result
    except subprocess.TimeoutExpired:
        raise DrillFailed("worker_process_timeout") from None
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def _worker_once(environment):
    _complete_worker(_launch_worker(environment))


def _job(store, job_id):
    with store.engine.connect() as conn:
        row = conn.execute(select(store.refresh_jobs).where(store.refresh_jobs.c.id == job_id)).mappings().one()
    return dict(row)


def _source_state(store, tenant, source_id):
    with store.engine.connect() as conn:
        source = dict(conn.execute(select(store.connections).where(store.connections.c.tenant_id == tenant,
                      store.connections.c.id == source_id)).mappings().one())
        heads = [dict(row) for row in conn.execute(select(store.inventory_heads).where(
            store.inventory_heads.c.tenant_id == tenant, store.inventory_heads.c.connection_id == source_id)
            .order_by(store.inventory_heads.c.scope_digest, store.inventory_heads.c.policy_digest)).mappings()]
        snapshots = conn.execute(select(func.count()).select_from(store.snapshots).where(
            store.snapshots.c.tenant_id == tenant, store.snapshots.c.connection_id == source_id)).scalar_one()
        publications = conn.execute(select(func.count()).select_from(store.publications).where(
            store.publications.c.tenant_id == tenant, store.publications.c.connection_id == source_id)).scalar_one()
    return source, heads, snapshots, publications


@contextmanager
def _manager(store, owner):
    previous = store.access_enforced
    store.access_enforced = True
    try:
        with principal_scope(owner):
            yield
    finally:
        store.access_enforced = previous


def _enqueue(store, tenant, source_id, owner):
    with _manager(store, owner):
        return RefreshQueue(store).enqueue(tenant, source_id)


def _wait_blocked(store, application_name, *, child=None):
    deadline = monotonic() + 15
    while monotonic() < deadline:
        with store.engine.connect() as conn:
            blocked = conn.execute(text("SELECT pid FROM pg_stat_activity WHERE application_name=:name "
                "AND datname=current_database() AND wait_event_type='Lock' "
                "AND query LIKE '%knowledge_connections%' LIMIT 1"), {"name": application_name}).first()
        if blocked:
            return
        if child is not None and child.poll() is not None:
            raise DrillFailed("worker_did_not_wait_for_source_lock")
        sleep(0.05)
    raise DrillFailed("source_lock_wait_timeout")


def _writer_revocation_race(report, store, raw_url, tenant, source_id, owner):
    """Actual blocked InventoryStore.begin with a REPEATABLE READ engine default."""
    contender = KnowledgeStore(database_url=raw_url, initialize_schema=False)
    original_engine = contender.engine
    contender.engine = original_engine.execution_options(isolation_level="REPEATABLE READ")
    contender.source_policy = store.source_policy
    contender.access_enforced = True
    inventory = InventoryStore(contender)
    queue = RefreshQueue(store)
    job = _enqueue(store, tenant, source_id, owner)
    claimed = queue.claim("fixture-race")
    if claimed is None or claimed["id"] != job["id"]:
        raise DrillFailed("race_claim_isolation")
    pin = adapter_profile(store.get_connection(tenant, source_id)["config"])[1]
    reached, finished = threading.Event(), threading.Event()
    result = {}

    def begin_after_wait():
        try:
            with principal_scope(owner), refresh_fence(job["id"], "fixture-race", claimed["attempt"]):
                with contender.engine.connect() as conn:
                    result["default_isolation"] = conn.get_isolation_level()
                    result["pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
                reached.set()
                result["manifest"] = inventory.begin(tenant, source_id,
                    scope={"namespace": "external_adapter", "selectors": ["provider:fixture", "scope:finance"]},
                    policy={"id": "external-adapter.inventory", "revision": pin},
                    watermark={"kind": "unknown", "value": None}, limits=LIMITS)
        except (KnowledgeAccessDenied, KnowledgeNotFound):
            result["refused"] = True
        except Exception:  # noqa: BLE001 - no arbitrary driver diagnostic leaves the private fixture
            result["unexpected_failure"] = True
        finally:
            finished.set()

    thread = threading.Thread(target=begin_after_wait, daemon=True)
    try:
        with store.engine.begin() as blocker:
            blocker.execute(select(store.connections.c.id).where(store.connections.c.id == source_id).with_for_update())
            thread.start()
            if not reached.wait(10):
                raise DrillFailed("inventory_race_start_timeout")
            deadline = monotonic() + 15
            while monotonic() < deadline:
                with store.engine.connect() as conn:
                    waiting = conn.execute(text("SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid=:pid AND wait_event_type='Lock' AND query LIKE '%knowledge_connections%' LIMIT 1"),
                        {"pid": result["pid"]}).first()
                if waiting:
                    break
                if finished.is_set():
                    raise DrillFailed("inventory_race_did_not_wait")
                sleep(0.05)
            else:
                raise DrillFailed("inventory_race_lock_timeout")
            blocker.execute(delete(store.source_grants).where(store.source_grants.c.tenant_id == tenant,
                store.source_grants.c.connection_id == source_id, store.source_grants.c.principal_type == "user",
                store.source_grants.c.principal_id == owner.subject))
        if not finished.wait(15):
            raise DrillFailed("inventory_race_finish_timeout")
        _check(report, "inventory_writer_rechecks_revoked_grant_after_real_source_lock_wait",
               result.get("default_isolation") == "REPEATABLE READ" and result.get("refused") is True
               and not {"manifest", "unexpected_failure"} & set(result),
               default_isolation="REPEATABLE READ", writer_isolation="READ COMMITTED")
        with store.engine.connect() as conn:
            staged = conn.execute(select(func.count()).select_from(store.inventory_runs).where(
                store.inventory_runs.c.claim["job_id"].as_string() == job["id"])).scalar_one()
        _check(report, "revoked_writer_does_not_stage_or_attribute", staged == 0
               and _job(store, job["id"])["result_inventory_id"] is None)
        queue.finish(job["id"], "fixture-race", attempt=claimed["attempt"], cancelled=True)
    finally:
        thread.join(timeout=15)
        contender.close()
        if thread.is_alive():
            raise DrillFailed("inventory_race_thread_not_stopped")
    store.grant_source_access(tenant, source_id, principal_type="user", principal_id=owner.subject, role="manage")


def _worker_revocation_race(report, store, tenant, source_id, owner, environment, adapter):
    job = _enqueue(store, tenant, source_id, owner)
    queue = RefreshQueue(store)
    claimed = queue.claim("fixture-permission-worker")
    if claimed is None or claimed["id"] != job["id"]:
        raise DrillFailed("permission_claim_isolation")
    before = _source_state(store, tenant, source_id)
    calls_before = len(adapter.observations())
    child = None
    try:
        with store.engine.begin() as blocker:
            blocker.execute(select(store.connections.c.id).where(store.connections.c.id == source_id).with_for_update())
            # Claim before the blocker: the parent queue intentionally SKIP
            # LOCKEDs sources, while this already claimed child must wait.
            child = _launch_worker(environment, claim=claimed)
            _wait_blocked(store, environment["PGAPPNAME"], child=child)
            blocker.execute(delete(store.source_grants).where(store.source_grants.c.tenant_id == tenant,
                store.source_grants.c.connection_id == source_id, store.source_grants.c.principal_type == "user",
                store.source_grants.c.principal_id == owner.subject))
        exit_code = _complete_worker(child, expected_codes=(1, 2))
        queue.finish(job["id"], claimed["worker_id"], attempt=claimed["attempt"],
                     error_code="fixture_authorization_refused" if exit_code == 1 else None,
                     cancelled=exit_code == 2)
    finally:
        if child is not None and child.poll() is None:
            _complete_worker(child, expected_codes=(1, 2))
    finished = _job(store, job["id"])
    _check(report, "actual_worker_revoked_direct_grant_prevents_all_adapter_calls",
           len(adapter.observations()) == calls_before and finished["status"] in {"retry_wait", "cancelled"}
           and finished["result_inventory_id"] is None and finished["result_snapshot_id"] is None,
           adapter_calls_after_revocation=0)
    _check(report, "revoked_worker_preserves_exact_current_head_and_clocks",
           _source_state(store, tenant, source_id) == before)


def qualify(report, *, output=None):
    raw_url, _url = _database_url()
    nonce = secrets.token_hex(6)
    tenant = "inventory_refresh_" + nonce
    owner = Principal("inventory_owner_" + nonce, frozenset(), frozenset({"knowledge.sources.manage"}))
    reference = "KNOWLEDGE_SOURCE_REFRESH_" + nonce.upper()
    source_id, baseline, store, adapter, server_thread = None, None, None, None, None
    report["fixture_ownership"] = {"tenant_id": tenant, "connection_id": None, "nonce": nonce}
    if output:
        _write_report(output, report)
    with tempfile.TemporaryDirectory(prefix="knowledge-inventory-refresh-") as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        try:
            store = KnowledgeStore(database_url=raw_url, local_files_root=directory, initialize_schema=False)
            ensure_current_schema(store)
            migration = run("status", store)
            with store.engine.connect() as conn:
                revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                active = conn.execute(select(func.count()).select_from(store.refresh_jobs).where(
                    store.refresh_jobs.c.status.in_(ACTIVE))).scalar_one()
                schedules = conn.execute(select(func.count()).select_from(store.refresh_schedules).where(
                    store.refresh_schedules.c.paused.is_(False))).scalar_one()
            _check(report, "current_migrated_disposable_postgres",
                   isinstance(migration["head"], str) and bool(migration["head"])
                   and revision == migration["head"] and migration["schema_matches"] is True
                   and "result_inventory_id" in {item["name"] for item in inspect(store.engine).get_columns(store.refresh_jobs.name)},
                   schema_revision=revision)
            _check(report, "global_worker_has_no_unrelated_active_jobs_or_schedules", active == schedules == 0)
            baseline = _census(store)
            adapter = FixtureAdapter(secrets.token_hex(24))
            server_thread = threading.Thread(target=adapter.serve_forever, daemon=True)
            server_thread.start()
            profile_file, policy_file = directory / "profile.json", directory / "policy.json"
            _private_json(profile_file, {"endpoint": f"http://127.0.0.1:{adapter.server_port}/discover",
                "provider": "fixture", "allowed_scopes": ["finance"], "protocol_version": "1.3",
                "revision": "synthetic.fixture.1", "source_identity": "synthetic.fixture." + nonce,
                "bearer_token": adapter.token})
            config = {"secret_ref": reference, "scope": "finance", "discovery_mode": "inventory",
                      "retain_inventory_payloads": True, "inventory_limits": LIMITS}
            selector = {key: value for key, value in config.items() if key != "inventory_limits"}
            policy_payload = {"version": 1, "sources": [{"tenant_id": tenant, "kind": "external_adapter",
                "selector": selector, "allowed_subjects": [owner.subject], "allowed_groups": []}]}
            _private_json(policy_file, policy_payload)
            environment = _worker_environment(policy_file, reference, profile_file, nonce)
            with _environment({reference + "_FILE": str(profile_file), "KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS": "1"}):
                source = store.create_connection(tenant, kind="external_adapter", name="Synthetic paged fixture", config=config)
                source_id = source["id"]
                report["fixture_ownership"]["connection_id"] = source_id
                if output:
                    _write_report(output, report)
                store.grant_source_access(tenant, source_id, principal_type="user", principal_id=owner.subject, role="manage")
                store.source_policy = SourcePolicy.from_json(json.dumps(policy_payload))
                initial = _enqueue(store, tenant, source_id, owner)
                _worker_once(environment)
                job = _job(store, initial["id"])
                _check(report, "actual_parent_child_worker_records_only_inventory_result", job["status"] == "succeeded"
                       and job["attempt"] == 1 and job["requested_by"] == owner.subject
                       and job["result_inventory_id"] is not None and job["result_snapshot_id"] is None)
                inventory = InventoryStore(store)
                manifest = inventory.status(tenant, source_id, job["result_inventory_id"])
                entity_page = inventory.page(tenant, source_id, manifest["id"], collection="entities")
                evidence_page = inventory.page(tenant, source_id, manifest["id"], collection="evidence")
                _check(report, "two_pages_exact_early_entity_late_evidence_readback", manifest["status"] == "committed"
                       and manifest["page_count"] == 2 and manifest["entity_count"] == manifest["evidence_count"] == 1
                       and [{key: value for key, value in item.items() if key != "digest"}
                            for item in entity_page["items"]] == [ENTITY]
                       and [{key: value for key, value in item.items() if key != "digest"}
                            for item in evidence_page["items"]] == [EVIDENCE]
                       and entity_page["current"] and evidence_page["current"], pages=2, entities=1, evidence=1)
                first_state = _source_state(store, tenant, source_id)
                _check(report, "native_completion_separates_scoped_clock_from_semantic_clocks", first_state[0]["status"] == "inventory_complete"
                       and first_state[0]["last_refresh_attempt_at"] is not None
                       and first_state[0]["last_refreshed_at"] is None and first_state[0]["last_complete_refresh_at"] is None
                       and first_state[1][0]["last_complete_scope_at"] == manifest["completed_at"]
                       and first_state[2:] == (0, 0))
                with _manager(store, owner):
                    schedule = RefreshSchedules(store).set(tenant, source_id,
                        {"cadence": "hourly", "timezone": "UTC", "minute": 0})
                with store.engine.begin() as conn:
                    conn.execute(update(store.refresh_schedules).where(store.refresh_schedules.c.id == schedule["id"])
                                 .values(next_run_at=utc_now()))
                adapter.mode = "fail_second"
                calls_before = len(adapter.observations())
                _worker_once(environment)
                with store.engine.connect() as conn:
                    failed = dict(conn.execute(select(store.refresh_jobs).where(store.refresh_jobs.c.schedule_id == schedule["id"])).mappings().one())
                    staged = [dict(row) for row in conn.execute(select(store.inventory_runs).where(
                        store.inventory_runs.c.claim["job_id"].as_string() == failed["id"])).mappings()]
                after_failure = _source_state(store, tenant, source_id)
                _check(report, "scheduled_late_page_failure_preserves_head_and_aborts_its_staging", failed["status"] == "retry_wait"
                       and failed["attempt"] == 1 and failed["result_inventory_id"] is None
                       and len(staged) == 1 and staged[0]["status"] == "aborted" and staged[0]["page_count"] == 1
                       and after_failure[1:] == first_state[1:]
                       and after_failure[0]["last_complete_refresh_at"] is None,
                       failed_pages=len(adapter.observations()) - calls_before, aborted_staging=1)
                adapter.mode = "success"
                with store.engine.begin() as conn:
                    conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == failed["id"])
                                 .values(next_run_at=utc_now()))
                _worker_once(environment)
                retried = _job(store, failed["id"])
                after_retry = _source_state(store, tenant, source_id)
                _check(report, "scheduled_retry_records_only_new_generation_inventory", retried["status"] == "succeeded"
                       and retried["attempt"] == 2 and retried["result_snapshot_id"] is None
                       and retried["result_inventory_id"] not in {None, manifest["id"], staged[0]["id"]}
                       and after_retry[1][0]["generation"] == first_state[1][0]["generation"] + 1
                       and after_retry[1][0]["inventory_id"] == retried["result_inventory_id"]
                       and after_retry[2:] == (0, 0))
                with _manager(store, owner):
                    RefreshSchedules(store).set(tenant, source_id, schedule["config"], paused=True)
                cancelled = _enqueue(store, tenant, source_id, owner)
                adapter.mode = "cancel_second"
                adapter.cancel_attempt = lambda: RefreshQueue(store).cancel(tenant, cancelled["id"])
                _worker_once(environment)
                adapter.cancel_attempt = None
                adapter.mode = "success"
                cancelled_job = _job(store, cancelled["id"])
                with store.engine.connect() as conn:
                    retired = [dict(row) for row in conn.execute(select(store.inventory_runs).where(
                        store.inventory_runs.c.claim["job_id"].as_string() == cancelled["id"])).mappings()]
                    aborts = [dict(row) for row in conn.execute(select(store.audit_events).where(
                        store.audit_events.c.tenant_id == tenant, store.audit_events.c.connection_id == source_id,
                        store.audit_events.c.event_type == "inventory.abort")).mappings()
                        if row["details"].get("job_id") == cancelled["id"]]
                after_cancel = _source_state(store, tenant, source_id)
                _check(report, "actual_parent_retires_only_cancelled_attempt_after_late_page_failure",
                       cancelled_job["status"] == "cancelled" and cancelled_job["result_inventory_id"] is None
                       and cancelled_job["result_snapshot_id"] is None and len(retired) == len(aborts) == 1
                       and retired[0]["status"] == "aborted" and retired[0]["page_count"] == 1
                       and aborts[0]["actor"] == "service"
                       and aborts[0]["details"] == {"inventory_id": retired[0]["id"], "job_id": cancelled["id"],
                                                  "attempt": 1, "reason": "refresh_attempt_retired"}
                       and after_cancel[1:] == after_retry[1:]
                       and after_cancel[0]["last_complete_refresh_at"] is None,
                       retired_staging=1, preserved_current_head=True)
                _writer_revocation_race(report, store, raw_url, tenant, source_id, owner)
                _worker_revocation_race(report, store, tenant, source_id, owner, environment, adapter)
                report["qualification"] = {"backend": "postgresql", "disposable_registry": True,
                    "fixture_provider": "synthetic_loopback", "real_worker_parent_and_child": True,
                    "adapter_calls": len(adapter.observations()), "source_row_values_retained": False,
                    "customer_source_access": False, "source_writes": 0, "models": 0,
                    "semantic_approval": False, "global_source_completion_qualified": False}
        finally:
            try:
                try:
                    if adapter is not None:
                        if server_thread is not None and server_thread.is_alive():
                            adapter.shutdown()
                        adapter.server_close()
                        if server_thread is not None:
                            server_thread.join(timeout=10)
                            _check(report, "owned_loopback_adapter_stopped", not server_thread.is_alive())
                finally:
                    try:
                        if store is not None and source_id is not None:
                            remaining = _cleanup_registry(store, tenant, source_id)
                            _check(report, "exact_owned_registry_fixture_removed", all(count == 0 for count in remaining.values()))
                    finally:
                        if store is not None and baseline is not None:
                            _check(report, "all_registry_table_counts_restored", _census(store) == baseline,
                                   registry_tables=len(baseline))
            finally:
                if store is not None:
                    store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Owner-only JSON receipt; contains fixture IDs, never DSN or tokens")
    arguments = parser.parse_args()
    report = {"schema_version": "inventory-refresh-qualification-v1", "result": "failed", "checks": [],
              "claim_limits": ["Synthetic contributor responses; no customer/vendor source qualification.",
                               "Actual PostgreSQL registry and parent/child worker execution.",
                               "Scoped inventory completion is separate from semantic approval and global freshness."]}
    started = monotonic()
    try:
        qualify(report, output=arguments.output)
        report["result"] = "passed"
    except Exception as exc:  # noqa: BLE001 - never print credential-bearing driver/process/HTTP diagnostics
        report["failure"] = {"type": type(exc).__name__}
        if isinstance(exc, DrillFailed):
            report["failure"]["check"] = exc.args[0]
    report["elapsed_seconds"] = round(monotonic() - started, 3)
    report["passed_checks"] = sum(item["passed"] for item in report["checks"])
    report["total_checks"] = len(report["checks"])
    if arguments.output:
        try:
            _write_report(arguments.output, report)
        except (OSError, DrillFailed, ValueError, TypeError):
            print(json.dumps({"result": "failed", "failure": "owner_only_report_write_refused"}))
            return 1
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
