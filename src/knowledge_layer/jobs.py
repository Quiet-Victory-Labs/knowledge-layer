"""PostgreSQL-backed, at-least-once source refresh queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from knowledge_layer.access_log import record_source_read
from knowledge_layer.refresh_fence import lease_is_current, validate_refresh_inventory
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
    utc_now,
    validate_tenant_id,
)

ACTIVE = ("queued", "running", "retry_wait")
TERMINAL = ("succeeded", "failed", "cancelled")
MAX_RETIRED_INVENTORIES = 20


def _later(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in (
        "id", "connection_id", "status", "requested_at", "started_at", "finished_at",
        "attempt", "max_attempts", "cancel_requested", "result_snapshot_id", "error_code",
        "result_inventory_id", "schedule_id",
    )}


def _require_attempt(attempt: int) -> None:
    if type(attempt) is not int or attempt < 1:
        raise KnowledgeValidationError("Worker mutations require a claimed refresh attempt")


class RefreshQueue:
    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.table = store.refresh_jobs

    def _operator_only(self) -> None:
        if self.store.access_enforced:
            raise KnowledgeAccessDenied("Worker operations require an operator process")

    def _lock_enqueue(self, conn) -> None:
        """Serialize global capacity decisions in the caller's transaction."""
        if self.store.backend == "postgresql":
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('knowledge_refresh_enqueue'))")

    def _enqueue_in_transaction(
        self, conn, tenant: str, connection_id: str, *, requested_by: str,
        now: str, max_pending: int = 100, schedule_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Caller holds the enqueue lock; schedule/job changes commit together."""
        source = self.store._lock_source(conn, tenant, connection_id)
        self.store._require_source_approval(tenant, source["kind"], source["config"])
        if source["kind"] == "external_adapter" and source["config"].get("discovery_mode") == "inventory":
            from knowledge_layer.inventory_refresh import require_inventory_requester

            require_inventory_requester(self.store, conn, tenant, source, requested_by)
        existing = conn.execute(select(self.table).where(
            self.table.c.tenant_id == tenant,
            self.table.c.connection_id == connection_id,
            self.table.c.status.in_(ACTIVE),
        ).limit(1)).mappings().first()
        if existing:
            return _public(dict(existing)), False
        pending = conn.execute(select(func.count()).select_from(self.table).where(
            self.table.c.status.in_(ACTIVE),
        )).scalar_one()
        if pending >= max_pending:
            raise KnowledgeConflict("Refresh queue is at capacity; retry later")
        row = {
            "id": f"job_{uuid4().hex}", "tenant_id": tenant,
            "connection_id": connection_id, "status": "queued",
            "requested_by": requested_by, "schedule_id": schedule_id,
            "requested_at": now, "next_run_at": now,
            "started_at": None, "finished_at": None, "lease_until": None,
            "worker_id": None, "attempt": 0, "max_attempts": 3,
            "cancel_requested": False, "result_snapshot_id": None, "result_inventory_id": None,
            "error_code": None, "updated_at": now,
        }
        conn.execute(insert(self.table).values(**row))
        self.store._audit(conn, tenant, connection_id, "source.refresh_requested",
                          requested_by, {"job_id": row["id"], "schedule_id": schedule_id})
        return _public(row), True

    def enqueue(self, tenant_id: str, connection_id: str, *, max_pending: int = 100) -> dict[str, Any]:
        """Create or reuse the one active job for a source, bounded by queue capacity."""
        tenant = validate_tenant_id(tenant_id)
        principal = self.store._require_source_manager()
        self.store._require_access(tenant, connection_id, manage=True)
        source = self.store._management_source(tenant, connection_id)
        self.store._require_source_approval(tenant, source["kind"], source["config"])
        now = utc_now()
        try:
            with self.store.engine.begin() as conn:
                self._lock_enqueue(conn)
                row, _created = self._enqueue_in_transaction(
                    conn, tenant, connection_id,
                    requested_by=principal.subject if principal else "operator",
                    now=now, max_pending=max_pending,
                )
                return row
        except IntegrityError:
            # The partial unique index closes the race between concurrent enqueues.
            with self.store.engine.begin() as conn:
                source = self.store._lock_source(conn, tenant, connection_id)
                self.store._require_source_approval(tenant, source["kind"], source["config"])
                if source["kind"] == "external_adapter" and source["config"].get("discovery_mode") == "inventory":
                    from knowledge_layer.inventory_refresh import require_inventory_requester

                    require_inventory_requester(self.store, conn, tenant, source,
                                                principal.subject if principal else "operator")
                existing = conn.execute(select(self.table).where(
                    self.table.c.tenant_id == tenant,
                    self.table.c.connection_id == connection_id,
                    self.table.c.status.in_(ACTIVE),
                ).limit(1)).mappings().first()
            if existing:
                return _public(dict(existing))
            raise

    def get(self, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        tenant = validate_tenant_id(tenant_id)
        with self.store.engine.connect() as conn:
            row = conn.execute(select(self.table).where(
                self.table.c.tenant_id == tenant, self.table.c.id == job_id,
            )).mappings().first()
        if row is None or not (self.store._has_access(tenant, row["connection_id"])
                              or self.store._has_access(tenant, row["connection_id"], manage=True)):
            return None
        record_source_read(tenant, row["connection_id"])
        return _public(dict(row))

    def active_for_connections(self, tenant_id: str, connection_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return only active jobs for an already authorized source list."""
        if not connection_ids:
            return {}
        tenant = validate_tenant_id(tenant_id)
        with self.store.engine.connect() as conn:
            rows = conn.execute(select(self.table).where(
                self.table.c.tenant_id == tenant,
                self.table.c.connection_id.in_(connection_ids),
                self.table.c.status.in_(ACTIVE),
            )).mappings().all()
        return {row["connection_id"]: _public(dict(row)) for row in rows}

    def cancel(self, tenant_id: str, job_id: str) -> dict[str, Any]:
        tenant = validate_tenant_id(tenant_id)
        self.store._require_source_manager()
        with self.store.engine.begin() as conn:
            row = conn.execute(select(self.table).where(
                self.table.c.tenant_id == tenant, self.table.c.id == job_id,
            ).with_for_update()).mappings().first()
            if row is None:
                raise KnowledgeNotFound("Refresh job was not found")
            self.store._require_access(tenant, row["connection_id"], manage=True)
            if row["status"] in TERMINAL:
                return _public(dict(row))
            now = utc_now()
            values = {"cancel_requested": True, "updated_at": now}
            if row["status"] in {"queued", "retry_wait"}:
                values.update(status="cancelled", finished_at=now, lease_until=None, worker_id=None)
            conn.execute(update(self.table).where(self.table.c.id == job_id).values(**values))
            principal = self.store._require_source_manager()
            self.store._audit(conn, tenant, row["connection_id"], "source.refresh_cancel_requested",
                              principal.subject if principal else "operator", {
                                  "job_id": job_id, "status": values.get("status", row["status"]),
                              })
            return _public({**dict(row), **values})

    def claim(self, worker_id: str, *, lease_seconds: int = 30,
              source_scope: tuple[str, tuple[str, ...]] | None = None) -> dict[str, Any] | None:
        self._operator_only()
        now = utc_now()
        with self.store.engine.begin() as conn:
            ready = or_(
                and_(self.table.c.status.in_(("queued", "retry_wait")), self.table.c.next_run_at <= now),
                and_(self.table.c.status == "running", self.table.c.lease_until <= now),
            )
            if source_scope is not None:
                tenant, identifiers = source_scope
                if not identifiers or len(identifiers) > 100:
                    raise KnowledgeValidationError("Worker source scope must contain 1-100 sources")
                ready = and_(ready, self.table.c.tenant_id == validate_tenant_id(tenant),
                             self.table.c.connection_id.in_(identifiers))
            statement = select(self.table).where(ready).order_by(
                self.table.c.next_run_at, self.table.c.requested_at,
            ).limit(1)
            row = conn.execute(statement).mappings().first()
            if row is None:
                return None
            # Match save_snapshot's source->job lock order. Locking a job first
            # and then writing its source could deadlock a fenced child commit.
            source_lock = select(self.store.connections.c.id).where(
                self.store.connections.c.tenant_id == row["tenant_id"],
                self.store.connections.c.id == row["connection_id"],
            ).with_for_update(skip_locked=True)
            if conn.execute(source_lock).first() is None:
                return None
            statement = select(self.table).where(self.table.c.id == row["id"], ready)
            if self.store.backend == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = conn.execute(statement).mappings().first()
            if row is None:
                return None
            row = dict(row)
            if row["cancel_requested"] or row["attempt"] >= row["max_attempts"]:
                status = "cancelled" if row["cancel_requested"] else "failed"
                conn.execute(update(self.table).where(self.table.c.id == row["id"]).values(
                    status=status, finished_at=now, worker_id=None, lease_until=None,
                    error_code=None if status == "cancelled" else "worker_lost",
                    updated_at=now, result_snapshot_id=None, result_inventory_id=None,
                ))
                self._retire_inventories(conn, row)
                if status == "failed":
                    conn.execute(update(self.store.connections).where(
                        self.store.connections.c.tenant_id == row["tenant_id"],
                        self.store.connections.c.id == row["connection_id"],
                    ).values(status="error", last_error="Refresh worker was lost after bounded retries.",
                             updated_at=now))
                self.store._audit(conn, row["tenant_id"], row["connection_id"],
                                  "source.refresh_attempt", "service", {
                                      "job_id": row["id"], "status": status,
                                      "error_code": None if status == "cancelled" else "worker_lost",
                                  })
                return None
            values = {
                "status": "running", "worker_id": worker_id,
                "lease_until": _later(lease_seconds), "attempt": row["attempt"] + 1,
                "started_at": row["started_at"] or now, "updated_at": now,
                "result_snapshot_id": None, "result_inventory_id": None,
            }
            conn.execute(update(self.table).where(self.table.c.id == row["id"]).values(**values))
            self._retire_inventories(conn, row)
            return {**row, **values}

    def _retire_inventories(self, conn, retired: dict[str, Any]) -> list[str]:
        """Abort bounded staging rows for an already fenced-out exact attempt.

        Caller holds source then job locks and has disabled that old generation.
        Local reconciliation deliberately does not require connector approval.
        No payload is deleted and no source/head/complete clock is changed.
        """
        attempt = retired["attempt"]
        if type(attempt) is not int or attempt < 1:
            return []
        current = conn.execute(select(self.table.c.attempt, self.table.c.worker_id,
                                      self.table.c.status, self.table.c.cancel_requested,
                                      self.table.c.lease_until).where(
            self.table.c.id == retired["id"], self.table.c.tenant_id == retired["tenant_id"],
            self.table.c.connection_id == retired["connection_id"],
        )).mappings().one()
        if (current["attempt"] == attempt and current["worker_id"] == retired["worker_id"]
                and current["status"] == "running" and current["cancel_requested"] is False
                and lease_is_current(current["lease_until"])):
            raise KnowledgeConflict("A live refresh attempt cannot be retired")
        runs = self.store.inventory_runs
        conditions = [runs.c.tenant_id == retired["tenant_id"],
                      runs.c.connection_id == retired["connection_id"], runs.c.status == "staging",
                      runs.c.claim["job_id"].as_string() == retired["id"],
                      runs.c.claim["attempt"].as_integer() == attempt]
        if retired["worker_id"] is not None:
            conditions.append(runs.c.claim["worker_id"].as_string() == retired["worker_id"])
        rows = conn.execute(select(runs.c.id, runs.c.claim).where(*conditions).order_by(runs.c.id)
                            .limit(MAX_RETIRED_INVENTORIES + 1).with_for_update()).mappings().all()
        if len(rows) > MAX_RETIRED_INVENTORIES:
            raise KnowledgeConflict("Retired refresh inventory count exceeds the reconciliation budget")
        identifiers = []
        for row in rows:
            claim = row["claim"]
            if (not isinstance(claim, dict) or set(claim) != {"job_id", "worker_id", "attempt"}
                    or type(claim.get("attempt")) is not int or claim["attempt"] != attempt
                    or claim["job_id"] != retired["id"] or not isinstance(claim["worker_id"], str)
                    or not 1 <= len(claim["worker_id"]) <= 80
                    or (retired["worker_id"] is not None and claim["worker_id"] != retired["worker_id"])):
                raise KnowledgeConflict("Retired refresh inventory claim is invalid")
            identifiers.append(row["id"])
        if not identifiers:
            return []
        if (conn.execute(select(self.store.inventory_heads.c.inventory_id).where(
                self.store.inventory_heads.c.inventory_id.in_(identifiers)).limit(1)).first() is not None
                or conn.execute(select(runs.c.id).where(
                    runs.c.previous_inventory_id.in_(identifiers)).limit(1)).first() is not None):
            raise KnowledgeConflict("A retired staging inventory is referenced by another manifest")
        updated = conn.execute(update(runs).where(runs.c.id.in_(identifiers),
                                                  runs.c.status == "staging").values(status="aborted"))
        if updated.rowcount != len(identifiers):
            raise KnowledgeConflict("Retired inventory reconciliation changed")
        for identifier in identifiers:
            self.store._audit(conn, retired["tenant_id"], retired["connection_id"],
                              "inventory.abort", "service", {
                                  "inventory_id": identifier, "job_id": retired["id"],
                                  "attempt": attempt, "reason": "refresh_attempt_retired",
                              })
        return identifiers

    def heartbeat(self, job_id: str, worker_id: str, *, attempt: int, lease_seconds: int = 30) -> bool:
        self._operator_only()
        _require_attempt(attempt)
        with self.store.engine.begin() as conn:
            row = conn.execute(select(self.table).where(
                self.table.c.id == job_id, self.table.c.worker_id == worker_id,
                self.table.c.attempt == attempt,
                self.table.c.status == "running", self.table.c.cancel_requested.is_(False),
            ).with_for_update()).mappings().first()
            now = utc_now()
            if row is None or not lease_is_current(row["lease_until"]):
                return False
            result = conn.execute(update(self.table).where(self.table.c.id == job_id)
                                  .values(lease_until=_later(lease_seconds), updated_at=now))
            return result.rowcount == 1

    def worker_job(self, job_id: str, worker_id: str, *, attempt: int | None = None) -> dict[str, Any] | None:
        self._operator_only()
        if attempt is not None:
            _require_attempt(attempt)
        statement = select(self.table).where(
            self.table.c.id == job_id, self.table.c.worker_id == worker_id,
            self.table.c.status == "running",
        )
        if attempt is not None:
            statement = statement.where(self.table.c.attempt == attempt,
                                        self.table.c.lease_until > utc_now(),
                                        self.table.c.cancel_requested.is_(False))
        with self.store.engine.connect() as conn:
            row = conn.execute(statement).mappings().first()
        return dict(row) if row else None

    def finish(self, job_id: str, worker_id: str, *, attempt: int, result_snapshot_id: str | None = None,
               result_inventory_id: str | None = None, error_code: str | None = None,
               cancelled: bool = False) -> bool:
        self._operator_only()
        _require_attempt(attempt)
        if result_snapshot_id is not None and result_inventory_id is not None:
            raise KnowledgeValidationError("Refresh finish requires one typed result")
        with self.store.engine.begin() as conn:
            scope = conn.execute(select(self.table.c.tenant_id, self.table.c.connection_id).where(
                self.table.c.id == job_id,
            )).mappings().first()
            if scope is None:
                return False
            source = conn.execute(select(self.store.connections.c.id, self.store.connections.c.config).where(
                self.store.connections.c.tenant_id == scope["tenant_id"],
                self.store.connections.c.id == scope["connection_id"],
            ).with_for_update()).mappings().first()
            if source is None:
                return False
            row = conn.execute(select(self.table).where(
                self.table.c.id == job_id, self.table.c.worker_id == worker_id,
                self.table.c.attempt == attempt,
                self.table.c.status == "running",
            ).with_for_update()).mappings().first()
            now = utc_now()
            if row is None or not lease_is_current(row["lease_until"]):
                return False
            if error_code is None and not cancelled and not row["cancel_requested"]:
                recorded_inventory = row["result_inventory_id"]
                inventory_mode = (source["config"] or {}).get("discovery_mode") == "inventory"
                if inventory_mode or recorded_inventory is not None or result_inventory_id is not None:
                    if (result_inventory_id is None or result_inventory_id != recorded_inventory
                            or result_snapshot_id is not None or row["result_snapshot_id"] is not None):
                        raise KnowledgeConflict("Refresh inventory result was not recorded by this generation")
                    validate_refresh_inventory(self.store, conn, row["tenant_id"], row["connection_id"],
                                               result_inventory_id, job_id=job_id,
                                               worker_id=worker_id, attempt=attempt)
            if cancelled or row["cancel_requested"]:
                status, next_run_at, error_code = "cancelled", now, None
            elif error_code is None:
                status, next_run_at = "succeeded", now
            elif row["attempt"] < row["max_attempts"]:
                status = "retry_wait"
                next_run_at = _later(min(60, 5 * 4 ** (row["attempt"] - 1)))
            else:
                status, next_run_at = "failed", now
            values = {
                "status": status, "next_run_at": next_run_at,
                "finished_at": now if status in TERMINAL else None,
                "lease_until": None, "worker_id": None, "updated_at": now,
                "result_snapshot_id": result_snapshot_id if status == "succeeded" else None,
                "result_inventory_id": result_inventory_id if status == "succeeded" else None,
                "error_code": error_code,
            }
            conn.execute(update(self.table).where(self.table.c.id == job_id).values(**values))
            if status != "succeeded":
                self._retire_inventories(conn, dict(row))
            if status == "failed":
                conn.execute(update(self.store.connections).where(
                    self.store.connections.c.tenant_id == row["tenant_id"],
                    self.store.connections.c.id == row["connection_id"],
                ).values(status="error", last_error="Refresh failed after bounded retries.", updated_at=now))
            self.store._audit(conn, row["tenant_id"], row["connection_id"],
                              "source.refresh_attempt", "service", {
                                  "job_id": job_id, "status": status,
                                  "result_snapshot_id": values["result_snapshot_id"],
                                  "result_inventory_id": values["result_inventory_id"],
                                  "error_code": error_code,
                              })
            if not lease_is_current(row["lease_until"]):
                raise KnowledgeConflict("Refresh lease expired before attempt completion")
            return True
