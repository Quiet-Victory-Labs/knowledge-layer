"""Durable attribution for a queued worker's source-native discovery attempt."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import select

from knowledge_layer.refresh_fence import assert_refresh_fence, current_refresh_claim
from knowledge_layer.source_permissions import digest
from knowledge_layer.source_policy import source_identity


@contextmanager
def source_io_attempt(store, job: dict):
    """Commit the start before source IO; completion never grants source authority."""
    tenant, source_id = job["tenant_id"], job["connection_id"]
    claim = current_refresh_claim()
    if claim is None or (claim["job_id"], claim["attempt"]) != (job["id"], job["attempt"]):
        raise ValueError("Source read attribution requires the exact queued attempt")
    read_id = "read_" + uuid4().hex
    with store.engine.begin() as conn:
        source = store._lock_source(conn, tenant, source_id)
        assert_refresh_fence(store, conn, tenant, source_id)
        details = {"read_id": read_id, "job_id": job["id"], "attempt": job["attempt"],
                   "operation": "metadata_discovery",
                   "source_identity_digest": digest(source_identity(source["kind"], source["config"]))}
        store._audit(conn, tenant, source_id, "source.native_read_started", job["requested_by"], details)
    outcome = "failed"
    try:
        yield
        outcome = "returned"
    finally:
        # A revoked grant/retired lease may prevent saving the result, but the
        # read already started. Retain its outcome without updating source state.
        with store.engine.begin() as conn:
            start = conn.execute(select(store.audit_events.c.id).where(
                store.audit_events.c.tenant_id == tenant,
                store.audit_events.c.connection_id == source_id,
                store.audit_events.c.event_type == "source.native_read_started",
                store.audit_events.c.details["read_id"].as_string() == read_id,
                store.audit_events.c.actor == job["requested_by"],
            )).first()
            if start is None:
                raise RuntimeError("Source read attribution start is unavailable")
            store._audit(conn, tenant, source_id, "source.native_read_finished", job["requested_by"],
                         {**details, "outcome": outcome})
