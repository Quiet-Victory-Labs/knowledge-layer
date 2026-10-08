"""Read-only build navigation for a customer's existing agent harness.

Snapshots, proposals and decisions remain the durable state. This projection
does not dispatch an agent, create a build run, read connected sources or make
a review decision. Evidence returned for its small work sample is exact stored
evidence from the pinned snapshot; the sample is not a full estate census.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from knowledge_layer.curation_handoff import maintenance_work
from knowledge_layer.mcp import MAX_CONTRACT_RESPONSE_BYTES
from knowledge_layer.store import KnowledgeConflict, KnowledgeNotFound


def _navigation(connection_id: str, track: str) -> dict[str, Any]:
    return {
        "mcp_tool": "next_work",
        "arguments": {"connection_id": connection_id, "track": track, "limit": 3, "offset": 0},
        "http_method": "GET",
        "http_path": "/agent/api/curation-work" if track == "meaning" else "/agent/api/contract-work",
        "http_query": {"connection_id": connection_id, "limit": 3, "offset": 0},
        "pagination": "Read small pages. Submitted drafts leave the current queue, so reload offset=0 after submitting; otherwise use the returned offset and more_available.",
        "snapshot_check": "Compare each returned snapshot_id to this handoff. Reload get_build_handoff if it changed.",
    }


def _source_state(source: dict[str, Any]) -> str:
    tracks = [source["interpretation"][track] for track in ("meaning", "contract")]
    if "refresh_active" in source["blockers"]:
        return "discovery_pending"
    if source["discovery"]["complete"] is not True:
        return "discovery_required"
    if any(track["status"] == "blocked" for track in tracks):
        return "blocked"
    if any(track["work"] for track in tracks):
        return "draft_work_available"
    if any(track["scan_complete"] is not True for track in tracks):
        return "remaining_work_unknown"
    if source["review"]["pending_count"]:
        return "human_review_required"
    if source["publication"]["state"] == "published_for_agents":
        return "published_context_available"
    return "human_publication_required" if source["publication"]["eligible"] else "attention_required"


def build_handoff(
    store: Any, tenant_id: str, connection_id: str | None = None,
    cursor: str | None = None, limit: int = 20,
) -> dict[str, Any]:
    """Derive bounded both-track navigation and exact sampled work evidence.

    Source-page cursors and access checks come from maintenance_work. Exact
    evidence is retrieved only for that authorized page and current snapshot.
    Transports must budget the complete response rather than clip any strings.
    """
    result = deepcopy(maintenance_work(
        store, tenant_id=tenant_id, connection_id=connection_id, cursor=cursor, limit=limit,
    ))
    tenant = result["tenant_id"]
    sources = []
    for source in result["sources"]:
        current = store.get_connection(tenant, source["connection_id"])
        if current is None:
            continue
        snapshot = current.get("snapshot") or {}
        pin = source["discovery"]
        if snapshot.get("id") != pin["snapshot_id"] or snapshot.get("version") != pin["snapshot_version"]:
            raise KnowledgeConflict("Source snapshot changed while preparing the build handoff; reload current work")
        evidence = {row["id"]: row for row in snapshot.get("evidence", []) if isinstance(row, dict) and row.get("id")}
        for track in ("meaning", "contract"):
            work = source["interpretation"][track]
            work["navigation"] = _navigation(source["connection_id"], track)
            work["sample_only"] = True
            for packet in work["work"]:
                handles = packet["evidence_ids"]
                if any(handle not in evidence for handle in handles):
                    raise KnowledgeConflict("Work evidence changed while preparing the build handoff; reload current work")
                packet["submission_tool"] = "propose_meaning" if track == "meaning" else "propose_contract"
                packet["evidence"] = [deepcopy(evidence[handle]) for handle in handles]
                packet["local_evidence_refs"] = [{
                    "connection_id": source["connection_id"], "snapshot_id": pin["snapshot_id"], "evidence_id": handle,
                } for handle in handles]
                packet["evidence_text_complete"] = True
        source["build_state"] = _source_state(source)
        source["human_acceptance"] = {
            "surface": "authenticated_browser", "agent_can_accept": False,
            "expected_snapshot_id": pin["snapshot_id"], "expected_snapshot_version": pin["snapshot_version"],
            "selection": "Review exact selected proposals and their evidence; approval covers only those claims.",
            "publication": "A steward publishes the reviewed current snapshot for agent use. Other drafts retain their own trust states.",
        }
        source["resume"] = {
            "mcp_tool": "get_build_handoff", "arguments": {"connection_id": source["connection_id"]},
            "discovery": "For queued discovery, the customer harness schedules a later check using get_workspace and get_refresh_job when enabled. Resume both work tracks only after discovery succeeds.",
            "after_acceptance": "Reload the current handoff; use resolve_context, get_contract or get_task_context with the same agent and current published context.",
        }
        sources.append(source)
    # Do not expose exact evidence after a grant is revoked during derivation.
    sources = [source for source in sources if store._has_access(tenant, source["connection_id"])]
    if connection_id is not None and not sources:
        raise KnowledgeNotFound("Connection was not found")
    if len(sources) != len(result["sources"]):
        result["total_authorized_sources"] = store.count_connections(tenant)
    result["sources"] = sources
    result["status"] = "empty" if not sources else "build_handoff_available"
    result["schema_version"] = "knowledge-build-handoff-v1"
    result["harness"] = {
        "dispatch_required": "customer_harness", "agent_dispatched": False,
        "build_run_created": False, "model_invoked": False,
        "resume_owner": "customer_harness", "human_review_required": True,
    }
    result["next_page"] = ({
        "mcp_tool": "get_build_handoff", "arguments": {"cursor": result["next_cursor"], "limit": limit},
        "http_path": "/agent/api/build-handoff", "http_query": {"cursor": result["next_cursor"], "limit": limit},
    } if result["next_cursor"] is not None else None)
    result["limits"] = {
        **result["limits"], "maintenance_projection_max_response_bytes": result["limits"]["max_response_bytes"],
        "max_response_bytes": MAX_CONTRACT_RESPONSE_BYTES,
        "response_budget": "The HTTP/MCP complete-contract budget applies after exact evidence is attached; oversized responses refuse without clipping. Reduce the source page or select one source.",
        "evidence_completeness": "Exact stored evidence for the returned work sample; source extraction, discovery and work coverage remain separately bounded.",
    }
    return result
