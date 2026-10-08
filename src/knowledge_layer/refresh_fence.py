"""Fence refresh writes to one live queue claim, never to a worker name alone.

The store calls the assertion inside each source mutation transaction *after*
locking the source row. The job row stays locked through commit so cancellation
or takeover cannot race the check. Unqueued, synchronous refreshes deliberately
have no fence; this is execution ownership, not an authorization mechanism.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update

from knowledge_layer.store import KnowledgeConflict


class RefreshFenceLost(KnowledgeConflict):
    """The refresh claim expired, changed generation, or was cancelled."""


@dataclass(frozen=True)
class RefreshFence:
    job_id: str
    worker_id: str
    attempt: int


_current: ContextVar[RefreshFence | None] = ContextVar("knowledge_refresh_fence", default=None)


def current_refresh_claim() -> dict | None:
    """Expose provenance to other registry writes without granting ownership."""
    fence = _current.get()
    return ({"job_id": fence.job_id, "worker_id": fence.worker_id, "attempt": fence.attempt}
            if fence else None)


def lease_is_current(value: str | None) -> bool:
    try:
        lease = datetime.fromisoformat(value)
        return lease.utcoffset() is not None and lease > datetime.now(UTC)
    except (ValueError, TypeError):
        return False


@contextmanager
def refresh_fence(job_id: str, worker_id: str, attempt: int):
    if (not isinstance(job_id, str) or not job_id or len(job_id) > 64
            or not isinstance(worker_id, str) or not worker_id or len(worker_id) > 80
            or type(attempt) is not int or attempt < 1):
        raise RefreshFenceLost("Refresh claim is unavailable")
    token = _current.set(RefreshFence(job_id, worker_id, attempt))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def assert_refresh_fence(store, conn, tenant_id: str, connection_id: str) -> None:
    """Require the current generation under the caller's locked source row.

SQLite exercises the predicates in tests; PostgreSQL provides the row-lock
serialization guarantee. This function intentionally does not open a second
transaction or query a connected enterprise source.
"""
    fence = _current.get()
    if fence is None:
        return
    table = store.refresh_jobs
    row = conn.execute(select(table).where(
        table.c.id == fence.job_id,
        table.c.tenant_id == tenant_id,
        table.c.connection_id == connection_id,
    ).with_for_update()).mappings().first()
    valid = (row is not None and row["status"] == "running"
             and row["worker_id"] == fence.worker_id and row["attempt"] == fence.attempt
             and row["cancel_requested"] is False)
    if not valid or not lease_is_current(row["lease_until"]):
        raise RefreshFenceLost("Refresh claim is no longer current")
    from knowledge_layer.refresh_authorization import require_refresh_requester
    from knowledge_layer.store import KnowledgeAccessDenied

    try:
        require_refresh_requester(store, conn, row)
    except KnowledgeAccessDenied:
        raise RefreshFenceLost("Refresh requester authority is no longer current") from None


def record_refresh_snapshot(store, conn, tenant_id: str, connection_id: str, snapshot_id: str) -> None:
    """Record this generation's actual result under the source/job commit locks."""
    fence = _current.get()
    if fence is None:
        return
    assert_refresh_fence(store, conn, tenant_id, connection_id)
    if not isinstance(snapshot_id, str) or not snapshot_id or len(snapshot_id) > 128:
        raise RefreshFenceLost("Refresh result snapshot is unavailable")
    table = store.refresh_jobs
    if conn.execute(select(table.c.result_inventory_id).where(table.c.id == fence.job_id)).scalar_one() is not None:
        raise RefreshFenceLost("Refresh attempt already recorded an inventory result")
    conn.execute(update(table).where(
        table.c.id == fence.job_id, table.c.tenant_id == tenant_id,
        table.c.connection_id == connection_id,
        table.c.worker_id == fence.worker_id, table.c.attempt == fence.attempt,
        table.c.status == "running", table.c.cancel_requested.is_(False),
    ).values(result_snapshot_id=snapshot_id))


def validate_refresh_inventory(
    store, conn, tenant_id: str, connection_id: str, inventory_id: str, *,
    job_id: str, worker_id: str, attempt: int,
) -> None:
    """Validate exact committed attribution under source->job->run locks.

    The caller already holds the source and generation's job lock. This does
    not infer a result from a connection's latest head or approve its meaning.
    """
    if not isinstance(inventory_id, str) or not inventory_id or len(inventory_id) > 64:
        raise RefreshFenceLost("Refresh result inventory is unavailable")
    runs, heads = store.inventory_runs, store.inventory_heads
    row = conn.execute(select(
        runs.c.id, runs.c.status, runs.c.claim, runs.c.scope_digest, runs.c.policy_digest,
        runs.c.manifest_digest, runs.c.base_generation,
    ).where(runs.c.id == inventory_id, runs.c.tenant_id == tenant_id,
            runs.c.connection_id == connection_id).with_for_update()).mappings().first()
    expected = {"job_id": job_id, "worker_id": worker_id, "attempt": attempt}
    claim = row["claim"] if row else None
    if (row is None or row["status"] != "committed" or not isinstance(claim, dict)
            or set(claim) != set(expected) or type(claim.get("attempt")) is not int or claim != expected
            or type(row["base_generation"]) is not int):
        raise RefreshFenceLost("Refresh inventory is not committed by this exact attempt")
    current = conn.execute(select(heads.c.inventory_id).where(
        heads.c.tenant_id == tenant_id, heads.c.connection_id == connection_id,
        heads.c.scope_digest == row["scope_digest"], heads.c.policy_digest == row["policy_digest"],
        heads.c.inventory_id == inventory_id, heads.c.manifest_digest == row["manifest_digest"],
        heads.c.generation == row["base_generation"] + 1,
    ).with_for_update()).first()
    if current is None:
        raise RefreshFenceLost("Refresh inventory no longer owns its current scoped head")


def record_refresh_inventory(store, conn, tenant_id: str, connection_id: str, inventory_id: str) -> None:
    """Atomically attribute this attempt's current committed discovery manifest."""
    fence = _current.get()
    if fence is None:
        return
    assert_refresh_fence(store, conn, tenant_id, connection_id)
    table = store.refresh_jobs
    existing = conn.execute(select(table.c.result_snapshot_id, table.c.result_inventory_id).where(
        table.c.id == fence.job_id)).mappings().one()
    if (existing["result_snapshot_id"] is not None
            or existing["result_inventory_id"] not in (None, inventory_id)):
        raise RefreshFenceLost("Refresh attempt already recorded a different typed result")
    validate_refresh_inventory(store, conn, tenant_id, connection_id, inventory_id,
                               job_id=fence.job_id, worker_id=fence.worker_id, attempt=fence.attempt)
    updated = conn.execute(update(table).where(
        table.c.id == fence.job_id, table.c.tenant_id == tenant_id,
        table.c.connection_id == connection_id, table.c.worker_id == fence.worker_id,
        table.c.attempt == fence.attempt, table.c.status == "running",
        table.c.cancel_requested.is_(False),
    ).values(result_inventory_id=inventory_id))
    if updated.rowcount != 1:
        raise RefreshFenceLost("Refresh inventory attribution changed")
    assert_refresh_fence(store, conn, tenant_id, connection_id)
