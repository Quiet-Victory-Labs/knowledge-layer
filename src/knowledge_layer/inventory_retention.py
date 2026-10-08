"""Explicit offline retention of unreferenced, already-aborted local inventories."""

from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from knowledge_layer.access import current_principal
from knowledge_layer.inventory import InventoryStore, inventory_digest
from knowledge_layer.jobs import ACTIVE, TERMINAL
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeStore,
    KnowledgeValidationError,
    validate_tenant_id,
)

SCHEMA_VERSION = "knowledge-inventory-retention-v1"
MAX_RUNS = 20
MAX_ROWS = 10_000
MAX_CLOCK_ROWS = 2000
_UTC_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _identifier(value: Any, label: str, maximum: int = 64) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise KnowledgeValidationError(f"A bounded explicit {label} is required")
    return value


def _limit(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise KnowledgeValidationError(f"{label} is outside the retention budget")
    return value


def _cutoff(value: datetime | str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise KnowledgeValidationError("An aware creation cutoff is required") from exc


def _stored_clock(value: Any) -> datetime:
    if not isinstance(value, str) or not _UTC_CLOCK.fullmatch(value):
        raise KnowledgeConflict("Aborted inventory creation clock is invalid")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KnowledgeConflict("Aborted inventory creation clock is invalid") from exc
    # This is the exact utc_now representation, including omitted zero fractions.
    if result.isoformat().replace("+00:00", "Z") != value:
        raise KnowledgeConflict("Aborted inventory creation clock is not canonical")
    return result


def _counter(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise KnowledgeConflict("Aborted inventory counters are invalid")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise KnowledgeConflict("Aborted inventory record digest is invalid")
    return value


class InventoryRetention:
    """Bounded registry-only preview/apply; no source approval, IO or clock writes."""

    def __init__(self, store: KnowledgeStore):
        self.store = store
        self.inventory = InventoryStore(store)

    def _operator_only(self) -> None:
        if self.store.access_enforced or current_principal() is not None:
            raise KnowledgeAccessDenied("Inventory retention requires an offline operator")

    @contextmanager
    def _transaction(self):
        self._operator_only()
        with self.store.engine.connect() as conn:
            # Final reads must see committed reference changes after source-lock waits.
            if conn.dialect.name == "postgresql":
                conn = conn.execution_options(isolation_level="READ COMMITTED")
            with conn.begin():
                if conn.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
                    conn.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
                yield conn

    def _arguments(self, tenant_id, connection_id, created_before, max_runs, max_rows):
        # validate_tenant_id has a demo fallback; retention deliberately has none.
        tenant = validate_tenant_id(_identifier(tenant_id, "tenant ID"))
        source = _identifier(connection_id, "connection ID")
        return (tenant, source, _cutoff(created_before),
                _limit(max_runs, "max_runs", MAX_RUNS), _limit(max_rows, "max_rows", MAX_ROWS))

    def _quiescent(self, conn, tenant, source) -> None:
        jobs = self.store.refresh_jobs
        row = conn.execute(select(jobs.c.status).where(
            jobs.c.tenant_id == tenant, jobs.c.connection_id == source,
            jobs.c.status.not_in(TERMINAL),
        ).order_by(jobs.c.id).limit(1).with_for_update()).first()
        if row is not None:
            code = "active refresh jobs" if row.status in ACTIVE else "unknown refresh job state"
            raise KnowledgeConflict(f"Inventory retention is blocked by {code}")

    def _candidates(self, conn, tenant, source, cutoff, max_runs):
        runs = self.inventory.runs
        # Parse canonical UTC clocks before comparing them. A finite scalar scan
        # handles zero microseconds correctly and refuses an uncertain population.
        clocks = list(conn.execute(select(runs.c.id, runs.c.created_at).where(
            runs.c.tenant_id == tenant, runs.c.connection_id == source,
            runs.c.status == "aborted",
        ).order_by(runs.c.id).limit(MAX_CLOCK_ROWS + 1)).mappings())
        if len(clocks) > MAX_CLOCK_ROWS:
            raise KnowledgeConflict("Aborted inventory clock scan exceeds the retention budget")
        eligible = []
        for row in clocks:
            identifier = _identifier(row["id"], "inventory ID")
            clock = _stored_clock(row["created_at"])
            if clock < cutoff:
                eligible.append((clock, identifier))
        eligible.sort()
        selected = eligible[:max_runs]
        boundary = eligible[max_runs:max_runs + 1]
        return [identifier for _clock, identifier in selected], boundary

    def _records(self, conn, table, identifier, columns, remaining):
        order = [table.c.page_index] if table is self.inventory.pages else [table.c.id]
        rows = list(conn.execute(select(*columns).where(table.c.inventory_id == identifier)
                                 .order_by(*order).limit(remaining + 1).with_for_update()).mappings())
        if len(rows) > remaining:
            raise KnowledgeConflict("Actual inventory rows exceed the retention budget")
        return [dict(row) for row in rows]

    def _preview(self, conn, tenant, source, cutoff, max_runs, max_rows):
        self._operator_only()
        # No connector approval check: withdrawn approval cannot prevent local cleanup.
        self.store._lock_source(conn, tenant, source)
        self._quiescent(conn, tenant, source)
        identifiers, boundary = self._candidates(conn, tenant, source, cutoff, max_runs)
        runs = self.inventory.runs
        rows = list(conn.execute(select(
            runs.c.id, runs.c.status, runs.c.created_at, runs.c.manifest_digest,
            runs.c.page_count, runs.c.entity_count, runs.c.evidence_count, runs.c.byte_count,
            runs.c.previous_inventory_id,
        ).where(runs.c.id.in_(identifiers), runs.c.tenant_id == tenant,
                runs.c.connection_id == source).order_by(runs.c.id).with_for_update()).mappings())
        if len(rows) != len(identifiers):
            raise KnowledgeConflict("The aborted inventory cohort changed")
        indexed = {row["id"]: dict(row) for row in rows}
        remaining = max_rows - len(identifiers)
        if remaining < 0:
            raise KnowledgeConflict("Actual inventory rows exceed the retention budget")
        summaries, fingerprints = [], []
        totals = {"runs": len(identifiers), "pages": 0, "entities": 0, "evidence": 0}
        for identifier in identifiers:
            row = indexed[identifier]
            if row["status"] != "aborted" or _stored_clock(row["created_at"]) >= cutoff:
                raise KnowledgeConflict("The aborted inventory cohort changed")
            _digest(row["manifest_digest"])
            if row["previous_inventory_id"] is not None:
                _identifier(row["previous_inventory_id"], "previous inventory ID")
            for key in ("page_count", "entity_count", "evidence_count", "byte_count"):
                _counter(row[key])
            if conn.execute(select(self.inventory.heads.c.inventory_id).where(
                    self.inventory.heads.c.inventory_id == identifier).limit(1)).first() is not None:
                raise KnowledgeConflict("An aborted inventory is referenced by a current head")
            if conn.execute(select(runs.c.id).where(
                    runs.c.previous_inventory_id == identifier).limit(1)).first() is not None:
                raise KnowledgeConflict("An aborted inventory is referenced by a predecessor pointer")
            if conn.execute(select(self.store.refresh_jobs.c.id).where(
                    self.store.refresh_jobs.c.result_inventory_id == identifier).limit(1)).first() is not None:
                raise KnowledgeConflict("An aborted inventory is referenced by a refresh result")
            pages = self._records(conn, self.inventory.pages, identifier, [
                self.inventory.pages.c.page_index, self.inventory.pages.c.digest,
                self.inventory.pages.c.byte_count, self.inventory.pages.c.entity_count,
                self.inventory.pages.c.evidence_count,
            ], remaining)
            remaining -= len(pages)
            entities = self._records(conn, self.inventory.entities, identifier, [
                self.inventory.entities.c.id, self.inventory.entities.c.page_index,
                self.inventory.entities.c.digest,
            ], remaining)
            remaining -= len(entities)
            evidence = self._records(conn, self.inventory.evidence, identifier, [
                self.inventory.evidence.c.id, self.inventory.evidence.c.page_index,
                self.inventory.evidence.c.digest,
            ], remaining)
            remaining -= len(evidence)
            actual = {"runs": 1, "pages": len(pages), "entities": len(entities), "evidence": len(evidence)}
            for key, records in (("pages", pages), ("entities", entities), ("evidence", evidence)):
                totals[key] += len(records)
                for record in records:
                    _digest(record["digest"])
                    _counter(record["page_index"])
                    if key != "pages":
                        _identifier(record["id"], "inventory child ID", 256)
            for page in pages:
                for key in ("byte_count", "entity_count", "evidence_count"):
                    _counter(page[key])
            if (row["page_count"] != len(pages) or row["entity_count"] != len(entities)
                    or row["evidence_count"] != len(evidence)
                    or row["byte_count"] != sum(page["byte_count"] for page in pages)
                    or len(entities) != sum(page["entity_count"] for page in pages)
                    or len(evidence) != sum(page["evidence_count"] for page in pages)):
                raise KnowledgeConflict("Stored inventory counters disagree with actual rows")
            page_indexes = {page["page_index"] for page in pages}
            if page_indexes != set(range(len(pages))) or any(
                    record["page_index"] not in page_indexes for record in entities + evidence):
                raise KnowledgeConflict("Aborted inventory child page references are invalid")
            entity_pages = Counter(record["page_index"] for record in entities)
            evidence_pages = Counter(record["page_index"] for record in evidence)
            for page in pages:
                if (entity_pages[page["page_index"]] != page["entity_count"]
                        or evidence_pages[page["page_index"]] != page["evidence_count"]):
                    raise KnowledgeConflict("Stored inventory page counters disagree with actual rows")
            summaries.append({"inventory_id": identifier, "created_at": row["created_at"],
                              "manifest_digest": row["manifest_digest"], "counts": actual})
            fingerprints.append({"run": row, "pages": pages, "entities": entities, "evidence": evidence})
        report = {
            "schema_version": SCHEMA_VERSION, "operation": "preview",
            "status": "ready" if identifiers else "empty", "tenant_id": tenant,
            "connection_id": source, "created_before": cutoff.isoformat().replace("+00:00", "Z"),
            "budgets": {"max_runs": max_runs, "max_rows": max_rows, "max_clock_rows": MAX_CLOCK_ROWS},
            "runs": summaries, "counts": {**totals, "total_rows": sum(totals.values())},
            "has_more": bool(boundary), "population_count": None if boundary else len(identifiers),
            "population_count_state": "unknown" if boundary else "exact",
            "source_reads_executed": False, "source_writes_executed": False,
            "registry_writes_executed": False, "source_clocks_changed": False,
        }
        report["preview_digest"] = inventory_digest({
            "report": report, "fingerprints": fingerprints,
            "boundary": [(clock.isoformat(), identifier) for clock, identifier in boundary],
        })
        return report

    def preview(self, tenant_id: str, connection_id: str, *, created_before: datetime | str,
                max_runs: int = MAX_RUNS, max_rows: int = 2000) -> dict[str, Any]:
        """Return a bounded content-free cohort; refusal never guesses unknown counts."""
        arguments = self._arguments(tenant_id, connection_id, created_before, max_runs, max_rows)
        with self._transaction() as conn:
            return self._preview(conn, *arguments)

    def apply(self, tenant_id: str, connection_id: str, *, created_before: datetime | str,
              expected_preview_digest: str, max_runs: int = MAX_RUNS,
              max_rows: int = 2000) -> dict[str, Any]:
        """Delete only an unchanged previewed cohort, with one atomic audit event."""
        arguments = self._arguments(tenant_id, connection_id, created_before, max_runs, max_rows)
        if not isinstance(expected_preview_digest, str) or not _DIGEST.fullmatch(expected_preview_digest):
            raise KnowledgeValidationError("An exact preview digest is required")
        with self._transaction() as conn:
            report = self._preview(conn, *arguments)
            if report["preview_digest"] != expected_preview_digest:
                raise KnowledgeConflict("Inventory retention preview changed; preview again")
            identifiers = [row["inventory_id"] for row in report["runs"]]
            if not identifiers:
                return {**report, "operation": "apply", "status": "empty"}
            for key, table in (("pages", self.inventory.pages), ("entities", self.inventory.entities),
                               ("evidence", self.inventory.evidence), ("runs", self.inventory.runs)):
                column = table.c.id if key == "runs" else table.c.inventory_id
                query = delete(table).where(column.in_(identifiers))
                if key == "runs":
                    query = query.where(table.c.tenant_id == arguments[0],
                                        table.c.connection_id == arguments[1], table.c.status == "aborted")
                deleted = conn.execute(query).rowcount
                if deleted != report["counts"][key]:
                    raise KnowledgeConflict("Inventory retention row counts changed")
                if conn.execute(select(column).where(column.in_(identifiers)).limit(1)).first() is not None:
                    raise KnowledgeConflict("Inventory retention deletion was incomplete")
            self.store._audit(conn, arguments[0], arguments[1], "inventory.retention", "operator", {
                "preview_digest": report["preview_digest"], "inventory_ids": identifiers,
                "counts": report["counts"], "created_before": report["created_before"],
            })
            self._operator_only()
            return {**report, "operation": "apply", "status": "applied", "registry_writes_executed": True}
