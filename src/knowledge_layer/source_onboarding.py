"""Explicitly enabled source onboarding for agent-first workflows."""

from __future__ import annotations

from typing import Any

from knowledge_layer.store import KnowledgeValidationError, validate_connection_config


def connector_options(store: Any, allowed_kinds: frozenset[str]) -> list[dict[str, Any]]:
    """Offer only connector kinds an operator has enabled for agent onboarding."""
    return [item for item in store.connector_types() if item["id"] in allowed_kinds]


def connect_source(
    store: Any, *, tenant_id: str, allowed_kinds: frozenset[str],
    kind: str, name: str, config: dict[str, Any], durable_jobs: bool = False,
) -> dict[str, Any]:
    if kind not in allowed_kinds:
        raise KnowledgeValidationError("This connector kind is not enabled for agent onboarding")
    normalized = validate_connection_config(kind, config)
    store._require_source_approval(tenant_id, kind, normalized)
    existing = store.find_connection_by_name(tenant_id, kind=kind, name=name)
    if existing is None:
        existing = store._find_management_source_by_name(tenant_id, kind=kind, name=name)
    if existing is not None:
        if existing["config"] != normalized:
            raise KnowledgeValidationError("A source with this name has a different scope; choose a new name or edit the existing connection")
        if existing.get("snapshot") and existing.get("status") != "error":
            return _refresh_summary(existing, existing["snapshot"], refresh_applied=False) | {"reused_existing": True}
        return refresh_source(store, tenant_id=tenant_id, allowed_kinds=allowed_kinds,
                              connection_id=existing["id"], durable_jobs=durable_jobs) | {"reused_existing": True}
    connection = store.create_connection(tenant_id, kind=kind, name=name, config=config)
    return refresh_source(store, tenant_id=tenant_id, allowed_kinds=allowed_kinds,
                          connection_id=connection["id"], durable_jobs=durable_jobs) | {"reused_existing": False}


def refresh_source(
    store: Any, *, tenant_id: str, allowed_kinds: frozenset[str], connection_id: str,
    durable_jobs: bool = False,
) -> dict[str, Any]:
    connection = store._management_source(tenant_id, connection_id)
    if connection["kind"] not in allowed_kinds:
        raise KnowledgeValidationError("This connector kind is not enabled for agent refresh")
    if durable_jobs:
        from knowledge_layer.jobs import RefreshQueue
        from knowledge_layer.mcp import _source_summary

        job = RefreshQueue(store).enqueue(tenant_id, connection_id)
        visible = store.get_connection(tenant_id, connection_id)
        summary = (_source_summary(visible) if visible is not None else
                   {"id": connection_id, "metadata_unavailable": True})
        return {"connection": summary, "job": job,
                "refresh_applied": False,
                "next_step": "Poll the refresh job before requesting current curation work."}
    result = store.refresh_connection(tenant_id, connection_id)
    if result["connection"].get("metadata_unavailable"):
        return {"connection": result["connection"], "refresh_applied": True,
                "metadata_unavailable": True, "next_step": "An operator must renew source metadata permission evidence before review."}
    if result.get("inventory") is not None:
        from knowledge_layer.mcp import _source_summary

        manifest = result["inventory"]
        return {"connection": _source_summary(result["connection"]),
                "inventory": manifest, "snapshot_id": None, "snapshot_version": None,
                "refresh_applied": True, "discovery_complete": None,
                "meaning_certified": False, "agent_ready": False,
                "counts": dict.fromkeys(("assets", "concepts", "relationships", "proposals", "evidence")),
                "counts_scope": "legacy_semantic_projection_unknown",
                "inventory_counts": {"pages": manifest["page_count"], "entities": manifest["entity_count"],
                                     "evidence": manifest["evidence_count"]},
                "next_step": "Read get_inventory_scopes, then select bounded inventory records for evidence-linked semantic curation."}
    return _refresh_summary(result["connection"], result.get("snapshot") or {},
                            refresh_applied=result["connection"].get("status") != "error")


def source_schedule(
    store: Any, *, tenant_id: str, allowed_kinds: frozenset[str], connection_id: str,
    config: dict[str, Any] | None = None, paused: bool = False,
) -> dict[str, Any]:
    """Manage recurring refresh metadata; execution needs the existing worker."""
    from knowledge_layer.schedules import RefreshSchedules
    from knowledge_layer.store import KnowledgeAccessDenied

    connection = store._management_source(tenant_id, connection_id)
    if connection["kind"] not in allowed_kinds:
        raise KnowledgeAccessDenied("This connector kind is not enabled for scheduled refresh")
    schedules = RefreshSchedules(store)
    schedule = (schedules.get(tenant_id, connection_id) if config is None else
                schedules.set(tenant_id, connection_id, config, paused=paused))
    return {"schedule": schedule, "execution_requires": "knowledge-layer-worker with this connector kind enabled"}


def _refresh_summary(connection: dict[str, Any], snapshot: dict[str, Any], *, refresh_applied: bool) -> dict[str, Any]:
    from knowledge_layer.mcp import _source_summary

    drift = (snapshot.get("drift") or {}) if refresh_applied else {}
    return {
        "connection": _source_summary(connection),
        "snapshot_id": snapshot.get("id"),
        "snapshot_version": snapshot.get("version"),
        "discovery_complete": snapshot.get("discovery_complete") is True,
        "meaning_certified": snapshot.get("meaning_certified") is True,
        "agent_ready": snapshot.get("agent_ready") is True,
        "curation_status": snapshot.get("curation_status"),
        "refresh_applied": refresh_applied,
        "counts": {
            key: len(snapshot.get(key) or [])
            for key in ("assets", "concepts", "relationships", "proposals", "evidence")
        },
        "drift": {
            "previous_snapshot_id": drift.get("previous_snapshot_id"),
            "added_assets": (drift.get("added_assets") or [])[:20],
            "removed_assets": (drift.get("removed_assets") or [])[:20],
            "changed_assets": (drift.get("changed_assets") or [])[:20],
        },
        "next_step": "Read list_curation_work and submit evidence-linked proposals; unresolved meaning remains pending review.",
    }
