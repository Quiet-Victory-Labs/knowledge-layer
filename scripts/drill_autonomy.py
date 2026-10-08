"""Qualify delegated publication on a disposable loopback PostgreSQL registry.

Reuses the registered inventory drill's guarded target and exact cleanup. A
SELECT-only fixture source is read through the normal connector; publication
does not execute source SQL. No enterprise data or real service identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import psycopg
from drill_inventory import (
    _census,
    _check,
    _cleanup_registry,
    _database_url,
    _refused,
    _write_report,
)
from drill_inventory import (
    qualify as qualify_inventory,
)
from drill_inventory_refresh import qualify as qualify_inventory_refresh
from drill_native_semantic import qualify as qualify_native_semantic
from sqlalchemy import select

from knowledge_layer import autonomy as autonomy_module
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.autonomy import ACTION, SCHEMA_VERSION, AutonomyRegistry
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.migrate import ensure_current_schema, run
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
)


@contextmanager
def disposable_database():
    """Task-owned isolated runtime; never borrow an ambient database target."""
    if os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_FILE"):
        raise RuntimeError("Managed drill requires no ambient database target")
    name = "knowledge-autonomy-" + secrets.token_hex(6)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="knowledge-autonomy-runtime-") as temporary:
        root, started = Path(temporary), False
        password = secrets.token_hex(24)
        for filename, content in {
            "postgres.env": "POSTGRES_USER=knowledge_owner\nPOSTGRES_DB=knowledge_build_autonomy\nPOSTGRES_PASSWORD=" + password + "\n",  # pragma: allowlist secret - public template with runtime-generated fixture credential
            "database-url": f"postgresql://knowledge_owner:{password}@127.0.0.1:{port}/knowledge_build_autonomy",
        }.items():
            fd = os.open(root / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
        old_container = os.environ.get("KNOWLEDGE_RECOVERY_CONTAINER")
        try:
            # A timed-out launch may already have created a container. Resolve
            # only our exact nonce label during teardown, never other services.
            started = True
            subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--env-file", str(root / "postgres.env"),
                "--label", "knowledge-layer.autonomy-drill=" + name,
                "-p", f"127.0.0.1:{port}:5432", "--memory", "256m", "--cpus", "1",
                "--tmpfs", "/var/lib/postgresql:rw,size=128m", "postgres:18.6-alpine"],
                check=True, capture_output=True, timeout=45)
            deadline = monotonic() + 20
            while monotonic() < deadline:
                ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "knowledge_owner",
                                        "-d", "knowledge_build_autonomy"], capture_output=True, timeout=5, check=False)
                if ready.returncode == 0:
                    break
                sleep(0.2)
            else:
                raise RuntimeError("Disposable database readiness timed out")
            os.environ["DATABASE_URL_FILE"] = str(root / "database-url")
            os.environ["KNOWLEDGE_RECOVERY_CONTAINER"] = name
            yield
        finally:
            os.environ.pop("DATABASE_URL_FILE", None)
            if old_container is None:
                os.environ.pop("KNOWLEDGE_RECOVERY_CONTAINER", None)
            else:
                os.environ["KNOWLEDGE_RECOVERY_CONTAINER"] = old_container
            if started:
                listing = ["docker", "container", "ls", "-a", "--filter", "name=^/" + name + "$",
                           "--format", "{{.Names}}"]
                present = subprocess.run(listing, check=False, capture_output=True, text=True, timeout=5)
                if present.returncode or present.stdout.strip() not in ("", name):
                    raise RuntimeError("Disposable container absence could not be verified")
                if present.stdout.strip():
                    ownership = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", name],
                        check=False, capture_output=True, text=True, timeout=5)
                    if ownership.returncode:
                        raise RuntimeError("Disposable container ownership could not be verified")
                    labels = json.loads(ownership.stdout)
                    if not isinstance(labels, dict) or labels.get("knowledge-layer.autonomy-drill") != name:
                        raise RuntimeError("Disposable container ownership changed")
                    subprocess.run(["docker", "stop", "--time", "5", name], check=True,
                                   capture_output=True, timeout=15)
                    # Docker's --rm deletion can finish shortly after stop
                    # returns. Success still requires verified absence.
                    deadline = monotonic() + 5
                    while True:
                        removed = subprocess.run(listing, check=False, capture_output=True, text=True, timeout=5)
                        if removed.returncode or removed.stdout.strip() not in ("", name):
                            raise RuntimeError("Disposable container removal could not be verified")
                        if not removed.stdout.strip():
                            break
                        if monotonic() >= deadline:
                            raise RuntimeError("Disposable container removal could not be verified")
                        sleep(0.05)


def _wait_for_database_lock(store):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with store.engine.connect() as conn:
            waiting = conn.exec_driver_sql("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid <> pg_backend_pid() "
                "AND wait_event_type='Lock' AND cardinality(pg_blocking_pids(pid))>0)").scalar_one()
        if waiting:
            return True
        sleep(0.01)
    return False


def _registry_fingerprints(store):
    """Bounded disposable fixture state, including updates with unchanged counts."""
    result = {}
    with store.engine.connect() as conn:
        for table in store.metadata.sorted_tables:
            rows = conn.execute(select(table).limit(1001)).mappings().all()
            if len(rows) > 1000:
                raise RuntimeError("Disposable registry exceeds fingerprint budget")
            encoded = sorted(json.dumps(dict(row), sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":"), allow_nan=False) for row in rows)
            result[table.name] = {"rows": len(rows), "digest": hashlib.sha256(
                json.dumps(encoded, ensure_ascii=False).encode()).hexdigest()}
    return result


def qualify(report, output):
    raw_url, url = _database_url()
    nonce = secrets.token_hex(6)
    tenant, table, role = "autonomy_qual_" + nonce, "kl_autonomy_" + nonce, "kl_reader_" + nonce
    owner = Principal("owner_" + nonce, frozenset({"stewards"}), frozenset({"knowledge.sources.manage"}))
    service = Principal("service_" + nonce, frozenset(),
                        frozenset({"knowledge.autonomy", "knowledge.sources.manage"}))
    source_id, created_fixture, baseline = None, False, None
    secret_variable = "KNOWLEDGE_SOURCE_AUTONOMY_" + nonce.upper() + "_FILE"  # pragma: allowlist secret - generated fixture reference
    report["fixture_ownership"] = {"tenant_id": tenant, "connection_id": None, "table": table, "role": role}
    _write_report(output, report)
    with tempfile.TemporaryDirectory(prefix="knowledge-autonomy-") as temporary:
        root = Path(temporary)
        store = KnowledgeStore(database_url=raw_url, local_files_root=root, initialize_schema=False)
        operator = KnowledgeStore(database_url=raw_url, local_files_root=root, initialize_schema=False)
        registry = AutonomyRegistry(store, execution_enabled=True)
        try:
            migration = run("upgrade", store)
            ensure_current_schema(store)
            _check(report, "migrated_current_schema",
                   isinstance(migration["head"], str) and bool(migration["head"])
                   and migration["revision"] == migration["head"] and migration["schema_matches"] is True)
            baseline = _census(store)
            password = secrets.token_hex(24)
            created_fixture = True
            with store.engine.begin() as conn:
                conn.exec_driver_sql(f'CREATE TABLE public."{table}" (record_id TEXT PRIMARY KEY, amount NUMERIC(12,2))')
                conn.exec_driver_sql(f'INSERT INTO public."{table}" VALUES (%s,%s)', [("known", 10), ("zero", 0), ("missing", None)])
                conn.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
                conn.exec_driver_sql(f'GRANT SELECT ON public."{table}" TO {role}')
            read_url = url.set(drivername="postgresql", username=role, password=password).render_as_string(hide_password=False)
            secret = root / "source-secret"
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(read_url)
            os.environ[secret_variable] = str(secret)
            config = {"secret_ref": secret_variable.removesuffix("_FILE"), "schema": "public", "tables": [table]}
            store.source_admin_group = "stewards"
            store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
                "tenant_id": tenant, "kind": "postgres", "selector": config,
                "allowed_subjects": [owner.subject, service.subject], "allowed_groups": []}]}))
            store.access_enforced = True
            with principal_scope(owner):
                source_id = store.create_connection(tenant, kind="postgres", name="Disposable autonomy fixture", config=config)["id"]
                report["fixture_ownership"]["connection_id"] = source_id
                _write_report(output, report)
                snapshot = store.refresh_connection(tenant, source_id)["snapshot"]
                asset = snapshot["assets"][0]
                evidence = next(e["id"] for e in snapshot["evidence"] if e.get("asset_id") == asset["id"])
                proposal = store.submit_contract_assertion(tenant, source_id, assertion_kind="dataset_description",
                    asset_id=asset["id"], payload={"description": "One disposable record per fixture identifier."},
                    evidence_ids=[evidence], rationale="Fixture steward reviewed native schema metadata.",
                    expected_snapshot_id=snapshot["id"])
                store.decide_proposal(tenant, source_id, proposal["id"], decision="approve",
                                      rationale="Fixture review covers only this schema description.")
            operator.grant_source_access(tenant, source_id, principal_type="user", principal_id=service.subject,
                                         role="manage", granted_by="fixture-operator")
            with principal_scope(owner):
                policy = registry.draft(tenant, source_id, {"schema_version": SCHEMA_VERSION, "action": ACTION,
                    "subject": service.subject, "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "max_publications": 1, "purpose": "Keep unchanged reviewed fixture metadata available to consumers."})
                registry.approve(tenant, source_id, revision=1, expected_digest=policy["digest"],
                                 rationale="Fixture steward delegates this exact unchanged publication class.")
                # A real native metadata refresh must preserve this narrow binding.
                snapshot = store.refresh_connection(tenant, source_id)["snapshot"]
            with store.engine.connect() as conn:
                decisions_before = [dict(row) for row in conn.execute(select(store.decisions).where(
                    store.decisions.c.tenant_id == tenant)).mappings()]
                clocks_before = dict(conn.execute(select(store.connections).where(
                    store.connections.c.id == source_id)).mappings().one())
            kwargs = {"revision": 1, "expected_digest": policy["digest"],
                      "expected_snapshot_id": snapshot["id"], "expected_snapshot_version": snapshot["version"]}

            def apply():
                with principal_scope(service):
                    return registry.apply(tenant, source_id, **kwargs)

            # Source/policy row locks must serialize duplicate attempts.
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(apply) for _ in range(2)]
                results = [future.result(timeout=15) for future in futures]
            _check(report, "concurrent_duplicate_exactly_one_publication",
                   sorted(row["result"] for row in results) == ["already_applied", "published"])
            with store.engine.connect() as conn:
                policies = conn.execute(select(store.autonomy_revisions).where(
                    store.autonomy_revisions.c.tenant_id == tenant)).mappings().all()
                runs = conn.execute(select(store.autonomy_runs).where(store.autonomy_runs.c.tenant_id == tenant)).mappings().all()
                pubs = conn.execute(select(store.publications).where(store.publications.c.tenant_id == tenant)).mappings().all()
                decisions_after = [dict(row) for row in conn.execute(select(store.decisions).where(
                    store.decisions.c.tenant_id == tenant)).mappings()]
                clocks_after = dict(conn.execute(select(store.connections).where(
                    store.connections.c.id == source_id)).mappings().one())
            _check(report, "atomic_persisted_usage_run_actor", len(policies) == len(runs) == len(pubs) == 1
                   and policies[0]["publication_count"] == 1 and runs[0]["owns_publication"] is True
                   and pubs[0]["published_by"] == runs[0]["actor"] == service.subject)
            _check(report, "no_review_or_source_clock_mutation", decisions_before == decisions_after
                   and clocks_before == clocks_after and all(row["source_executed"] is False for row in results))
            with principal_scope(owner):
                newer = store.refresh_connection(tenant, source_id)["snapshot"]
            newer_kwargs = {**kwargs, "expected_snapshot_id": newer["id"], "expected_snapshot_version": newer["version"]}
            with principal_scope(service):
                _check(report, "budget_exhaustion_refused", _refused(
                    lambda: registry.apply(tenant, source_id, **newer_kwargs), (KnowledgeConflict,)))
            # Give a reviewed replacement policy another publication budget.
            with principal_scope(owner):
                policy2 = registry.draft(tenant, source_id, {**policy["payload"], "max_publications": 2}, expected_revision=1)
                registry.approve(tenant, source_id, revision=2, expected_digest=policy2["digest"],
                                 rationale="Fixture steward reviews the replacement delegation.")
                queue = RefreshQueue(store)
                job = queue.enqueue(tenant, source_id)
            replacement = {**newer_kwargs, "revision": 2, "expected_digest": policy2["digest"]}
            with principal_scope(service):
                _check(report, "queued_refresh_blocks_publication", _refused(
                    lambda: registry.apply(tenant, source_id, **replacement), (KnowledgeConflict,)))
            with principal_scope(owner):
                queue.cancel(tenant, job["id"])
                approved_sources = store.source_policy
                store.source_policy = SourcePolicy(())
                _check(report, "inspection_after_connector_approval_withdrawal",
                       registry.get(tenant, source_id)["policy"]["revision"] == 2)
                withdrawn = registry.revoke(tenant, source_id, revision=2, expected_digest=policy2["digest"],
                                            reason="Fixture steward activates the source delegation kill switch.")
                _check(report, "kill_switch_after_connector_approval_withdrawal",
                       withdrawn["result"] == "withdrawn" and withdrawn["withdrawn_publications"] == 1)
                store.source_policy = approved_sources
            _check(report, "replacement_kill_switch_withdraws_prior_owned_publication",
                   withdrawn["withdrawn_publications"] == 1)
            with principal_scope(service):
                _check(report, "withdrawn_policy_refuses_retry", _refused(
                    lambda: registry.apply(tenant, source_id, **replacement), (KnowledgeConflict,)))
            with principal_scope(owner):
                policy3 = registry.draft(tenant, source_id, policy2["payload"], expected_revision=2)
                registry.approve(tenant, source_id, revision=3, expected_digest=policy3["digest"],
                                 rationale="Fixture steward authorizes the next reviewed generation.")
            latest = {**replacement, "revision": 3, "expected_digest": policy3["digest"]}

            # Pause the public grant-revocation path after its real source lock.
            # An apply started meanwhile must wait, then see the committed denial.
            locked, release = Event(), Event()
            original_lock = operator._lock_source

            def paused_lock(*args, **kw):
                row = original_lock(*args, **kw)
                locked.set()
                if not release.wait(10):
                    raise RuntimeError("fixture_lock_timeout")
                return row

            operator._lock_source = paused_lock
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    revoke = executor.submit(operator.revoke_source_access, tenant, source_id,
                        principal_type="user", principal_id=service.subject, revoked_by="fixture-operator")
                    if not locked.wait(5):
                        raise RuntimeError("fixture_revoke_lock_timeout")

                    def revoked_apply():
                        with principal_scope(service):
                            return _refused(lambda: registry.apply(tenant, source_id, **latest),
                                            (KnowledgeAccessDenied, KnowledgeNotFound))

                    attempt = executor.submit(revoked_apply)
                    waiting = _wait_for_database_lock(store)
                    release.set()
                    revoke.result(timeout=10)
                    denied = attempt.result(timeout=10)
                _check(report, "grant_revocation_serializes_and_denies_waiting_apply", waiting and denied)
            finally:
                release.set()
                operator._lock_source = original_lock
            operator.grant_source_access(tenant, source_id, principal_type="user", principal_id=service.subject,
                                         role="manage", granted_by="fixture-operator")
            before = _registry_fingerprints(store)
            original_audit, original_clock = store._audit, autonomy_module._clock
            expired = Event()

            def expire_on_last_audit(*args, **kw):
                result = original_audit(*args, **kw)
                if args[3] == "autonomy.apply":
                    expired.set()
                return result

            def controlled_clock():
                return datetime.now(UTC) + (timedelta(days=2) if expired.is_set() else timedelta())

            store._audit, autonomy_module._clock = expire_on_last_audit, controlled_clock
            try:
                with principal_scope(service):
                    denied = _refused(lambda: registry.apply(tenant, source_id, **latest), (KnowledgeConflict,))
                _check(report, "expiry_after_final_audit_rolls_back_every_table", expired.is_set()
                       and denied and _registry_fingerprints(store) == before)
            finally:
                store._audit, autonomy_module._clock = original_audit, original_clock
            # Optional populated dump/restore uses the existing registered drill.
            # Container selection is explicit operator configuration, never a source field.
            if os.getenv("KNOWLEDGE_RECOVERY_CONTAINER"):
                environment = {**os.environ, "KNOWLEDGE_RECOVERY_USER": url.username,
                               "KNOWLEDGE_RECOVERY_DATABASE": url.database}
                restored = subprocess.run([sys.executable, str(Path(__file__).with_name("drill_postgres_recovery.py"))],
                    env=environment, capture_output=True, text=True, timeout=45, check=True)
                recovery = json.loads(restored.stdout)
                _check(report, "populated_all_table_backup_restore", recovery["result"] == "passed"
                       and recovery["connection_count"] >= 1 and recovery["snapshot_count"] >= 1,
                       recovery_report=recovery["report"])
            with psycopg.connect(read_url) as conn, conn.cursor() as cursor:
                cursor.execute("BEGIN READ ONLY")
                cursor.execute(f'SELECT record_id, amount FROM public."{table}" ORDER BY record_id')
                rows = cursor.fetchall()
                cursor.execute("SELECT has_table_privilege(current_user, %s, 'INSERT')", ("public." + table,))
                writable = cursor.fetchone()[0]
                conn.rollback()
            _check(report, "source_read_only_and_exact_rows_preserved",
                   writable is False and rows == [("known", Decimal("10.00")),
                                                   ("missing", None), ("zero", Decimal("0.00"))])
        finally:
            store.access_enforced = False
            try:
                try:
                    if source_id:
                        remaining = _cleanup_registry(store, tenant, source_id)
                        _check(report, "all_owned_registry_rows_removed", all(value == 0 for value in remaining.values()),
                               remaining_owned_rows=remaining)
                finally:
                    if created_fixture:
                        try:
                            with store.engine.begin() as conn:
                                conn.exec_driver_sql(f'DROP TABLE IF EXISTS public."{table}"')
                        finally:
                            with store.engine.begin() as conn:
                                conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
                if baseline is not None:
                    after = _census(store)
                    _check(report, "all_registry_table_totals_unchanged", after == baseline,
                           registry_tables=len(baseline))
            finally:
                os.environ.pop(secret_variable, None)
                try:
                    operator.close()
                finally:
                    store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--managed", action="store_true", help="Create and remove a disposable Docker PostgreSQL runtime")
    parser.add_argument("--include-inventory", action="store_true",
                        help="Also qualify inventory persistence/currentness in the same guarded runtime")
    parser.add_argument("--include-inventory-refresh", action="store_true",
                        help="Also qualify real paged adapter workers and permission-lock races; includes inventory migration")
    parser.add_argument("--include-native-semantic", action="store_true",
                        help="Also qualify the dbt sample with two native scopes through actual PostgreSQL workers")
    args = parser.parse_args()
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {"schema_version": "autonomy-qualification-v1", "result": "running", "checks": []}

    def run_qualification():
        if args.include_inventory or args.include_inventory_refresh or args.include_native_semantic:
            inventory_report = {"schema_version": "inventory-qualification-v1", "result": "running", "checks": []}
            report["inventory_qualification"] = inventory_report
            try:
                qualify_inventory(inventory_report)
            except Exception:
                inventory_report["result"] = "failed"
                raise
            inventory_report["result"] = "passed"
            inventory_report["total_checks"] = len(inventory_report["checks"])
        if args.include_inventory_refresh:
            refresh_report = {"schema_version": "inventory-refresh-qualification-v1", "result": "running", "checks": []}
            report["inventory_refresh_qualification"] = refresh_report
            try:
                qualify_inventory_refresh(refresh_report)
            except Exception:
                refresh_report["result"] = "failed"
                raise
            refresh_report["result"] = "passed"
            refresh_report["total_checks"] = len(refresh_report["checks"])
        if args.include_native_semantic:
            native_report = {"schema_version": "native-semantic-qualification-v1", "result": "running", "checks": []}
            report["native_semantic_qualification"] = native_report
            try:
                qualify_native_semantic(native_report, output=None)
            except Exception:
                native_report["result"] = "failed"
                raise
            native_report["result"] = "passed"
            native_report["total_checks"] = len(native_report["checks"])
        qualify(report, args.output)

    try:
        if args.managed:
            with disposable_database():
                run_qualification()
        else:
            run_qualification()
        report["result"] = "passed"
    except Exception as exc:  # noqa: BLE001 -- suppress arbitrary driver diagnostics at CLI boundary
        report["result"] = "failed"
        report["error_type"] = type(exc).__name__
    report["total_checks"] = len(report["checks"])
    try:
        _write_report(args.output, report)
    except (OSError, ValueError, TypeError, RuntimeError):
        print(json.dumps({"result": "failed", "error_type": "ReportWriteFailure"}))
        return 1
    summary = {"result": report["result"], "checks": report["total_checks"], "report": str(args.output)}
    if report["result"] == "failed":
        # Fixed phase names and numeric indexes diagnose CI without copying
        # arbitrary driver errors, fixture names or check details into logs.
        summary["phases"] = {}
        for phase in ("inventory_qualification", "inventory_refresh_qualification", "native_semantic_qualification"):
            if phase in report:
                checks = report[phase]["checks"]
                summary["phases"][phase] = {"checks": len(checks),
                    "failed_indexes": [index for index, item in enumerate(checks) if item.get("passed") is False]}
    print(json.dumps(summary))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
