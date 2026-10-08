"""One bounded PostgreSQL refresh worker; run separately from the web process."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from uuid import uuid4

from sqlalchemy import select

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.migrate import ensure_current_schema
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.refresh_fence import (
    RefreshFenceLost,
    assert_refresh_fence,
    record_refresh_inventory,
    record_refresh_snapshot,
    refresh_fence,
)
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeStore, validate_tenant_id


def _store() -> KnowledgeStore:
    url = operator_secret("DATABASE_URL")
    if not url:
        raise RuntimeError("Worker requires DATABASE_URL or DATABASE_URL_FILE")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        # Default to the managed profile if the worker mode is omitted. The
        # loopback demo opts in explicitly and has no operator source policy.
        auth_mode = os.getenv("KNOWLEDGE_AUTH_MODE", "oidc")
        if auth_mode in {"oidc", "iap"}:
            store.source_policy = SourcePolicy.from_environment()
            store.source_policy_loader = SourcePolicy.from_environment
        elif auth_mode == "local":
            from knowledge_layer.local_profile import owner_source_policy

            store.source_policy = owner_source_policy(store)
            store.source_policy_loader = lambda: owner_source_policy(store)
        elif auth_mode != "demo":
            raise RuntimeError("Unknown worker authentication mode")
        ensure_current_schema(store)
    except Exception:
        store.close()
        raise
    return store


def _execute(job_id: str, worker_id: str, attempt: int) -> int:
    old_handler = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal, _frame):
        raise RefreshFenceLost("Refresh worker interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        store = _store()
    except BaseException:
        signal.signal(signal.SIGTERM, old_handler)
        raise
    try:
        job = RefreshQueue(store).worker_job(job_id, worker_id, attempt=attempt)
        if job is None or job["cancel_requested"]:
            return 2
        scope = worker_scope()
        if scope is not None and (job["tenant_id"] != scope[0] or job["connection_id"] not in scope[1]):
            return 2
        enabled = {item.strip() for item in os.getenv("KNOWLEDGE_ENABLED_KINDS", "").split(",") if item.strip()}
        if not RefreshSchedules(store).job_allowed(job, enabled_kinds=enabled):
            return 2
        source = store.get_connection(job["tenant_id"], job["connection_id"])
        if source is None or source["kind"] not in enabled:
            return 1
        with refresh_fence(job_id, worker_id, attempt):
            from knowledge_layer.source_io_audit import source_io_attempt

            with source_io_attempt(store, job):
                result = store.refresh_connection(job["tenant_id"], job["connection_id"])
            if result["connection"].get("status") != "error":
                snapshot = result.get("snapshot")
                inventory = result.get("inventory")
                if (snapshot is not None) + (inventory is not None) != 1:
                    return 1
                inventory_mode = (source["kind"] == "external_adapter"
                                  and source["config"].get("discovery_mode") == "inventory")
                if inventory_mode != (inventory is not None):
                    return 1
                artifact = inventory if inventory is not None else snapshot
                maximum = 64 if inventory is not None else 128
                if (not isinstance(artifact, dict) or not isinstance(artifact.get("id"), str)
                        or not 1 <= len(artifact["id"]) <= maximum):
                    return 1
                # The store normally records the snapshot atomically. A verified
                # unchanged refresh can return the previous snapshot instead.
                with store.engine.begin() as conn:
                    conn.execute(select(store.connections.c.id).where(
                        store.connections.c.tenant_id == job["tenant_id"],
                        store.connections.c.id == job["connection_id"],
                    ).with_for_update()).first()
                    assert_refresh_fence(store, conn, job["tenant_id"], job["connection_id"])
                    recorded = conn.execute(select(store.refresh_jobs.c.result_snapshot_id,
                                                   store.refresh_jobs.c.result_inventory_id).where(
                        store.refresh_jobs.c.id == job_id,
                    )).mappings().one()
                    if inventory is not None:
                        if recorded["result_snapshot_id"] is not None or recorded["result_inventory_id"] != artifact["id"]:
                            return 1
                        record_refresh_inventory(store, conn, job["tenant_id"], job["connection_id"], artifact["id"])
                    else:
                        if recorded["result_inventory_id"] is not None:
                            return 1
                        if recorded["result_snapshot_id"] is None:
                            record_refresh_snapshot(store, conn, job["tenant_id"], job["connection_id"], artifact["id"])
                        elif recorded["result_snapshot_id"] != artifact["id"]:
                            return 1
        return 0 if result["connection"].get("status") != "error" else 1
    except RefreshFenceLost:
        return 2
    except Exception:  # noqa: BLE001 - source and driver details must not enter worker output.
        return 1
    finally:
        store.close()
        signal.signal(signal.SIGTERM, old_handler)


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _run_claimed(
    store: KnowledgeStore, queue: RefreshQueue, job: dict, worker_id: str,
    *, timeout_seconds: int, stopping: list[bool],
) -> None:
    from knowledge_layer.connector_isolation import coordinator_environment

    environment = None
    if store is not None:
        source = store.get_connection(job["tenant_id"], job["connection_id"])
        if source is None:
            queue.finish(job["id"], worker_id, attempt=job["attempt"], error_code="source_unavailable")
            return
        try:
            environment = coordinator_environment(source["kind"], source["config"])
        except (ValueError, ConnectorError):
            queue.finish(job["id"], worker_id, attempt=job["attempt"], error_code="source_credentials_unavailable")
            return
    child = subprocess.Popen(
        [sys.executable, "-m", "knowledge_layer.worker", "execute", job["id"], worker_id, str(job["attempt"])],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
        start_new_session=True,
    )
    started = time.monotonic()
    last_heartbeat = started
    cancelled = False
    error_code: str | None = None
    try:
        while child.poll() is None:
            if stopping[0]:
                error_code = "worker_interrupted"
                _stop_child(child)
                break
            if time.monotonic() - started >= timeout_seconds:
                error_code = "timeout"
                _stop_child(child)
                break
            if time.monotonic() - last_heartbeat >= 5:
                enabled = {item.strip() for item in os.getenv("KNOWLEDGE_ENABLED_KINDS", "").split(",") if item.strip()}
                if store is not None and not RefreshSchedules(store).job_allowed(job, enabled_kinds=enabled):
                    cancelled = True
                    _stop_child(child)
                    break
                if not queue.heartbeat(job["id"], worker_id, attempt=job["attempt"]):
                    cancelled = True
                    _stop_child(child)
                    break
                last_heartbeat = time.monotonic()
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        _stop_child(child)
    if error_code is None and child.returncode == 2:
        cancelled = True
    if error_code is None and not cancelled and child.returncode != 0:
        error_code = "curation_failed"
    snapshot_id, inventory_id = None, None
    if error_code is None and not cancelled:
        current = queue.worker_job(job["id"], worker_id, attempt=job["attempt"])
        if current is None:
            cancelled = True
        else:
            snapshot_id = current.get("result_snapshot_id")
            inventory_id = current.get("result_inventory_id")
        if current is not None and (snapshot_id is not None) + (inventory_id is not None) != 1:
            error_code = "missing_result" if snapshot_id is None and inventory_id is None else "ambiguous_result"
            snapshot_id, inventory_id = None, None
    try:
        queue.finish(job["id"], worker_id, attempt=job["attempt"], result_snapshot_id=snapshot_id,
                     result_inventory_id=inventory_id, error_code=error_code, cancelled=cancelled)
    except RefreshFenceLost:
        if inventory_id is None or error_code is not None or cancelled:
            raise
        # Another completed listing can win after the child exits. Refuse the
        # stale attribution and retry without changing the newer scoped head.
        queue.finish(job["id"], worker_id, attempt=job["attempt"], error_code="inventory_result_stale")


def run(*, once: bool = False, poll_seconds: float = 2, timeout_seconds: int = 300) -> int:
    if not 0.1 <= poll_seconds <= 60 or not 30 <= timeout_seconds <= 900:
        raise ValueError("Worker polling and timeout settings are outside safe bounds")
    if not any(item.strip() for item in os.getenv("KNOWLEDGE_ENABLED_KINDS", "").split(",")):
        raise RuntimeError("Worker requires an explicit connector allowlist")
    store = _store()
    queue = RefreshQueue(store)
    schedules = RefreshSchedules(store)
    enabled = {item.strip() for item in os.getenv("KNOWLEDGE_ENABLED_KINDS", "").split(",") if item.strip()}
    worker_id = f"worker_{uuid4().hex}"
    scope = worker_scope()
    stopping = [False]

    def stop(_signal: int, _frame: object) -> None:
        stopping[0] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping[0]:
            schedules.dispatch_due(enabled, source_scope=scope)
            job = queue.claim(worker_id, source_scope=scope)
            if job is not None:
                _run_claimed(store, queue, job, worker_id,
                             timeout_seconds=timeout_seconds, stopping=stopping)
                if once:
                    break
            elif once:
                break
            else:
                time.sleep(poll_seconds)
    finally:
        store.close()
    return 0


def worker_scope() -> tuple[str, tuple[str, ...]] | None:
    raw = os.getenv("KNOWLEDGE_WORKER_CONNECTION_IDS")
    tenant = os.getenv("KNOWLEDGE_WORKER_TENANT")
    if raw is None and tenant is None:
        return None
    if not raw or not tenant:
        raise ValueError("Dedicated workers require both tenant and connection IDs")
    identifiers = tuple(raw.split(","))
    if (not 1 <= len(identifiers) <= 100 or len(set(identifiers)) != len(identifiers)
            or any(not re.fullmatch(r"conn_[A-Za-z0-9_-]{1,123}", value) for value in identifiers)):
        raise ValueError("Dedicated worker source scope is invalid")
    return validate_tenant_id(tenant), identifiers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("run", "execute"), default="run")
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("worker_id", nargs="?")
    parser.add_argument("attempt", nargs="?", type=int)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("KNOWLEDGE_REFRESH_TIMEOUT_SECONDS", "300")))
    args = parser.parse_args()
    if args.mode == "execute":
        if not args.job_id or not args.worker_id or args.attempt is None or args.attempt < 1:
            parser.error("execute requires job_id, worker_id and positive attempt")
        return _execute(args.job_id, args.worker_id, args.attempt)
    try:
        return run(once=args.once, poll_seconds=args.poll_seconds,
                   timeout_seconds=args.timeout_seconds)
    except (ValueError, RuntimeError):
        parser.exit(2, "Refresh worker could not start; inspect private deployment logs\n")


if __name__ == "__main__":
    raise SystemExit(main())
