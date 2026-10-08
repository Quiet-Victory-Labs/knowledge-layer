"""Current source authority for persisted jobs, independent of execution leases."""

from __future__ import annotations

from sqlalchemy import select

from knowledge_layer.access import Principal
from knowledge_layer.store import KnowledgeAccessDenied


def require_refresh_requester(store, conn, job) -> None:
    policy = store._current_source_policy()
    if policy is None and not store.access_enforced:
        return
    source = conn.execute(select(store.connections).where(
        store.connections.c.tenant_id == job["tenant_id"],
        store.connections.c.id == job["connection_id"],
    )).mappings().first()
    if source is None or policy is None:
        raise KnowledgeAccessDenied("Refresh source authority is unavailable")
    actor = job["requested_by"]
    grants = store.source_grants.c
    direct = conn.execute(select(grants.connection_id).where(
        grants.tenant_id == job["tenant_id"], grants.connection_id == job["connection_id"],
        grants.principal_type == "user", grants.principal_id == actor, grants.role == "manage",
    ).limit(1)).first()
    # Workers may rely on a current direct subject grant, never group claims
    # copied from an earlier browser request. The single-owner profile follows
    # the same direct-subject policy contract without querying the registry.
    approved = policy.allows(job["tenant_id"], source["kind"], source["config"],
                             Principal(actor, frozenset(), frozenset()))
    if actor == "operator" or direct is None or not approved:
        raise KnowledgeAccessDenied("Refresh requester authority is no longer current")
