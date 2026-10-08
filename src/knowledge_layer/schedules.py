"""Bounded recurring source refreshes on the existing registry and worker.

Calendar schedules use the first fold of repeated wall times and skip nonexistent
times. Hourly schedules use both repeated hours. Monthly days clamp to month end.
Missed occurrences coalesce into one job; the next due time is strictly future.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, exists, insert, select, text, update

from knowledge_layer.access_log import record_source_read
from knowledge_layer.jobs import ACTIVE, RefreshQueue
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
    validate_tenant_id,
)


def validate_schedule_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise KnowledgeValidationError("A schedule configuration object is required")
    cadence = config.get("cadence")
    if cadence not in ("hourly", "daily", "weekly", "monthly"):
        raise KnowledgeValidationError("Schedule cadence must be hourly, daily, weekly or monthly")
    required = {"cadence", "timezone", "minute"}
    if cadence != "hourly":
        required.add("hour")
    if cadence == "weekly":
        required.add("weekday")
    if cadence == "monthly":
        required.add("day")
    if not required <= config.keys() or config.keys() - required - {"freshness_max_age_seconds"}:
        raise KnowledgeValidationError("Schedule configuration has missing or unknown fields")
    zone = config["timezone"]
    if (not isinstance(zone, str) or not zone or len(zone) > 120
            or zone in {"localtime", "posixrules"} or zone.startswith(("posix/", "right/"))):
        raise KnowledgeValidationError("Schedule timezone must be an IANA timezone")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise KnowledgeValidationError("Schedule timezone must be an IANA timezone") from None
    bounds = {"minute": (0, 59), "hour": (0, 23), "weekday": (0, 6), "day": (1, 31),
              "freshness_max_age_seconds": (60, 2678400)}
    for key, (lower, upper) in bounds.items():
        if key in config and (type(config[key]) is not int or not lower <= config[key] <= upper):
            raise KnowledgeValidationError(f"Schedule {key} is outside its integer bounds")
    return dict(config)


def _now(value: datetime | None) -> datetime:
    value = value if value is not None else datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise KnowledgeValidationError("An aware schedule clock is required")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _wall_times(wall: datetime, zone: ZoneInfo, *, both_folds: bool = False) -> set[datetime]:
    result = set()
    for fold in (0, 1) if both_folds else (0,):
        candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        # ZoneInfo permits nonexistent times; a round trip detects and skips them.
        if candidate.astimezone(zone).replace(tzinfo=None) == wall:
            result.add(candidate)
    return result


def next_due(config: dict[str, Any], now: datetime) -> datetime:
    """Return the earliest scheduled instant strictly after an aware clock."""
    config = validate_schedule_config(config)
    now = _now(now)
    zone = ZoneInfo(config["timezone"])
    local = now.astimezone(zone).replace(tzinfo=None)
    candidates: set[datetime] = set()
    cadence = config["cadence"]
    try:
        if cadence == "hourly":
            start = local.replace(minute=config["minute"], second=0, microsecond=0)
            # Bounded horizon also covers historical whole-day offset changes.
            for offset in range(-26, 50):
                candidates.update(_wall_times(start + timedelta(hours=offset), zone, both_folds=True))
        elif cadence in ("daily", "weekly"):
            start = local.replace(hour=config["hour"], minute=config["minute"], second=0, microsecond=0)
            for offset in range(22):
                wall = start + timedelta(days=offset)
                if cadence == "daily" or wall.weekday() == config["weekday"]:
                    candidates.update(_wall_times(wall, zone))
        else:
            for offset in range(15):
                month_index = local.year * 12 + local.month - 1 + offset
                year, month = divmod(month_index, 12)
                month += 1
                day = min(config["day"], calendar.monthrange(year, month)[1])
                wall = datetime(year, month, day, config["hour"], config["minute"])  # noqa: DTZ001 - intentional wall time
                candidates.update(_wall_times(wall, zone))
    except (ValueError, OverflowError):
        raise KnowledgeValidationError("Schedule clock is outside the supported calendar") from None
    future = [value for value in candidates if value > now]
    if not future:
        raise KnowledgeValidationError("No future schedule time exists in the bounded calendar horizon")
    return min(future)


def _freshness(source: dict[str, Any], config: dict[str, Any], now: datetime) -> dict[str, Any]:
    maximum = config.get("freshness_max_age_seconds")
    complete = source.get("last_complete_refresh_at")
    result = {"state": "not_configured" if maximum is None else "unknown",
              "max_age_seconds": maximum, "age_seconds": None,
              "last_complete_refresh_at": complete,
              "last_refresh_attempt_at": source.get("last_refresh_attempt_at")}
    if maximum is not None and complete is not None:
        try:
            clock = datetime.fromisoformat(complete)
            if clock.utcoffset() is None or clock > now:
                return result
            age = (now - clock).total_seconds()
        except (TypeError, ValueError):
            return result
        result.update(age_seconds=age, state="within_window" if age <= maximum else "overdue")
    return result


class RefreshSchedules:
    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.table = store.refresh_schedules
        self.queue = RefreshQueue(store)

    def _source(self, conn, tenant: str, connection_id: str) -> dict[str, Any] | None:
        row = conn.execute(select(self.store.connections).where(
            self.store.connections.c.tenant_id == tenant,
            self.store.connections.c.id == connection_id,
        )).mappings().first()
        return dict(row) if row else None

    def _direct_manage(self, conn, tenant: str, connection_id: str, subject: str) -> bool:
        grants = self.store.source_grants.c
        return conn.execute(select(grants.connection_id).where(
            grants.tenant_id == tenant, grants.connection_id == connection_id,
            grants.principal_type == "user", grants.principal_id == subject,
            grants.role == "manage",
        ).limit(1)).first() is not None

    @staticmethod
    def _public(row: dict[str, Any], source: dict[str, Any], now: datetime) -> dict[str, Any]:
        return {**{key: row.get(key) for key in (
            "id", "connection_id", "config", "paused", "owner_subject", "created_at",
            "updated_at", "next_run_at", "last_enqueued_at", "last_job_id", "last_error",
        )}, "source_status": source["status"],
            "refresh_error": bool(source.get("last_error")),
            "freshness": _freshness(source, row["config"], now)}

    def get(self, tenant_id: str, connection_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        tenant = validate_tenant_id(tenant_id)
        self.store._require_source_manager()
        self.store._require_access(tenant, connection_id, manage=True)
        with self.store.engine.connect() as conn:
            source = self._source(conn, tenant, connection_id)
            if source is None:
                raise KnowledgeNotFound("Connection was not found")
            self.store._require_source_approval(tenant, source["kind"], source["config"])
            row = conn.execute(select(self.table).where(
                self.table.c.tenant_id == tenant, self.table.c.connection_id == connection_id,
            )).mappings().first()
        record_source_read(tenant, connection_id)
        return self._public(dict(row), source, _now(now)) if row else None

    def set(self, tenant_id: str, connection_id: str, config: dict[str, Any],
            paused: bool = False, *, now: datetime | None = None) -> dict[str, Any]:
        tenant = validate_tenant_id(tenant_id)
        config = validate_schedule_config(config)
        if type(paused) is not bool:
            raise KnowledgeValidationError("Schedule paused must be a boolean")
        principal = self.store._require_source_manager()
        self.store._require_access(tenant, connection_id, manage=True)
        owner = principal.subject if principal else "operator"
        if principal and owner == "operator":
            raise KnowledgeAccessDenied("The operator schedule owner is reserved for trusted processes")
        clock = _now(now)
        stamp = _stamp(clock)
        with self.store.engine.begin() as conn:
            if self.store.backend == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                             {"key": f"knowledge_schedule:{tenant}:{connection_id}"})
            source = self._source(conn, tenant, connection_id)
            if source is None:
                raise KnowledgeNotFound("Connection was not found")
            self.store._require_source_approval(tenant, source["kind"], source["config"])
            if principal and not self._direct_manage(conn, tenant, connection_id, owner):
                raise KnowledgeAccessDenied("Recurring refresh requires a direct user manage grant")
            previous = conn.execute(select(self.table).where(
                self.table.c.tenant_id == tenant, self.table.c.connection_id == connection_id,
            ).with_for_update()).mappings().first()
            values = {"config": config, "paused": paused, "owner_subject": owner,
                      "updated_at": stamp, "last_error": None,
                      "next_run_at": None if paused else _stamp(next_due(config, clock))}
            if previous:
                row = {**dict(previous), **values}
                conn.execute(update(self.table).where(self.table.c.id == row["id"]).values(**values))
            else:
                row = {"id": f"schedule_{uuid4().hex}", "tenant_id": tenant,
                       "connection_id": connection_id, "created_at": stamp,
                       "last_enqueued_at": None, "last_job_id": None, **values}
                conn.execute(insert(self.table).values(**row))
            if paused:
                self._cancel_scheduled(conn, row["id"], stamp)
            self.store._audit(conn, tenant, connection_id, "source.refresh_schedule_set", owner,
                              {"schedule_id": row["id"], "paused": paused, "cadence": config["cadence"]})
        return self._public(row, source, clock)

    def _cancel_scheduled(self, conn, schedule_id: str, stamp: str) -> None:
        jobs = self.store.refresh_jobs
        conn.execute(update(jobs).where(jobs.c.schedule_id == schedule_id,
                                       jobs.c.status.in_(("queued", "retry_wait"))).values(
            status="cancelled", cancel_requested=True, finished_at=stamp,
            worker_id=None, lease_until=None, updated_at=stamp,
        ))
        conn.execute(update(jobs).where(jobs.c.schedule_id == schedule_id,
                                       jobs.c.status == "running").values(
            cancel_requested=True, updated_at=stamp,
        ))

    def _blocked_reason(self, conn, row: dict[str, Any], enabled_kinds: set[str]) -> str | None:
        source = self._source(conn, row["tenant_id"], row["connection_id"])
        if source is None:
            return "source_missing"
        if source["kind"] not in enabled_kinds:
            return "connector_disabled"
        try:
            self.store._require_source_approval(row["tenant_id"], source["kind"], source["config"])
        except KnowledgeAccessDenied:
            return "source_not_approved"
        if row["owner_subject"] != "operator" and not self._direct_manage(
            conn, row["tenant_id"], row["connection_id"], row["owner_subject"],
        ):
            return "owner_grant_revoked"
        if source["kind"] == "external_adapter" and source["config"].get("discovery_mode") == "inventory":
            from knowledge_layer.inventory_refresh import require_inventory_requester

            try:
                require_inventory_requester(self.store, conn, row["tenant_id"], source, row["owner_subject"])
            except KnowledgeAccessDenied:
                return "inventory_owner_not_approved"
        return None

    def _pause_blocked(self, conn, row: dict[str, Any], reason: str, stamp: str) -> None:
        conn.execute(update(self.table).where(self.table.c.id == row["id"]).values(
            paused=True, next_run_at=None, last_error=reason, updated_at=stamp,
        ))
        self._cancel_scheduled(conn, row["id"], stamp)
        self.store._audit(conn, row["tenant_id"], row["connection_id"],
                          "source.refresh_schedule_blocked", "service",
                          {"schedule_id": row["id"], "error_code": reason})

    def dispatch_due(self, enabled_kinds: set[str], limit: int = 20, *,
                     max_pending: int = 100, now: datetime | None = None,
                     source_scope: tuple[str, tuple[str, ...]] | None = None) -> list[dict[str, Any]]:
        self.queue._operator_only()
        if type(limit) is not int or not 1 <= limit <= 20:
            raise KnowledgeValidationError("Schedule dispatch limit must be between 1 and 20")
        if type(max_pending) is not int or not 1 <= max_pending <= 100:
            raise KnowledgeValidationError("Refresh capacity must be between 1 and 100")
        clock = _now(now)
        stamp = _stamp(clock)
        results = []
        with self.store.engine.begin() as conn:
            self.queue._lock_enqueue(conn)
            jobs = self.store.refresh_jobs.c
            has_active = exists(select(jobs.id).where(
                jobs.tenant_id == self.table.c.tenant_id,
                jobs.connection_id == self.table.c.connection_id,
                jobs.status.in_(ACTIVE),
            ).correlate(self.table))
            statement = select(self.table).where(
                self.table.c.paused.is_(False), self.table.c.next_run_at <= stamp,
            )
            if source_scope is not None:
                tenant, identifiers = source_scope
                if not identifiers or len(identifiers) > 100:
                    raise KnowledgeValidationError("Worker source scope must contain 1-100 sources")
                statement = statement.where(self.table.c.tenant_id == validate_tenant_id(tenant),
                                            self.table.c.connection_id.in_(identifiers))
            statement = statement.order_by(case((has_active, 0), else_=1),
                       self.table.c.next_run_at, self.table.c.id).limit(limit)
            if self.store.backend == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            for value in conn.execute(statement).mappings().all():
                row = dict(value)
                reason = self._blocked_reason(conn, row, enabled_kinds)
                try:
                    following = _stamp(next_due(row["config"], clock))
                except KnowledgeValidationError:
                    reason = reason or "invalid_schedule"
                    following = None
                if reason:
                    self._pause_blocked(conn, row, reason, stamp)
                    results.append({"schedule_id": row["id"], "status": "blocked", "error_code": reason})
                    continue
                try:
                    job, created = self.queue._enqueue_in_transaction(
                        conn, row["tenant_id"], row["connection_id"], requested_by=row["owner_subject"],
                        now=stamp, max_pending=max_pending, schedule_id=row["id"],
                    )
                except KnowledgeConflict:
                    # Keep the occurrence due; the regular worker poll will retry.
                    conn.execute(update(self.table).where(self.table.c.id == row["id"]).values(
                        last_error="queue_capacity", updated_at=stamp,
                    ))
                    results.append({"schedule_id": row["id"], "status": "capacity"})
                    continue
                changes = {"next_run_at": following, "last_job_id": job["id"],
                           "last_error": None, "updated_at": stamp}
                if created:
                    changes["last_enqueued_at"] = stamp
                conn.execute(update(self.table).where(self.table.c.id == row["id"]).values(**changes))
                results.append({"schedule_id": row["id"], "job_id": job["id"],
                                "status": "enqueued" if created else "coalesced"})
        return results

    def job_allowed(self, job: dict[str, Any], *, enabled_kinds: set[str]) -> bool:
        """Recheck scheduled authorization before a child performs source reads."""
        self.queue._operator_only()
        if not job.get("schedule_id"):
            from knowledge_layer.refresh_authorization import require_refresh_requester

            try:
                with self.store.engine.begin() as conn:
                    require_refresh_requester(self.store, conn, job)
            except KnowledgeAccessDenied:
                return False
            return True
        with self.store.engine.begin() as conn:
            value = conn.execute(select(self.table).where(
                self.table.c.id == job["schedule_id"], self.table.c.tenant_id == job["tenant_id"],
                self.table.c.connection_id == job["connection_id"],
            ).with_for_update()).mappings().first()
            if value is None or value["paused"]:
                return False
            row = dict(value)
            reason = self._blocked_reason(conn, row, enabled_kinds)
            if reason:
                self._pause_blocked(conn, row, reason, _stamp(_now(None)))
                return False
        return True
