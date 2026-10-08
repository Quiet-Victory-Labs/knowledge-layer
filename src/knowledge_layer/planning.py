"""Authorized planning entry points; source execution stays with the harness."""

from __future__ import annotations

from typing import Any

from knowledge_layer.inventory_citations import inventory_citation_operation
from knowledge_layer.store import KnowledgeConflict, KnowledgeNotFound, KnowledgeValidationError


@inventory_citation_operation()
def metric_plan(store: Any, *, tenant_id: str, connection_id: str,
                assertion_id: str, request: dict[str, Any]) -> dict[str, Any]:
    from knowledge_layer.metric_planner import plan_metric_query
    from knowledge_layer.metric_semantics import metric_payload_digest, validate_metric_payload

    source = store.get_connection(tenant_id, connection_id)
    snapshot = (source or {}).get("snapshot") or {}
    if not source or snapshot.get("agent_ready") is not True:
        raise KnowledgeNotFound("Current published metric was not found")
    matches = [item for item in snapshot.get("proposals", [])
               if isinstance(item, dict) and item.get("id") == assertion_id
               and item.get("assertion_kind") == "metric" and item.get("status") == "approved"
               and item.get("effective_on_current_snapshot") is True]
    if len(matches) != 1:
        raise KnowledgeNotFound("Current published metric was not found")
    proposal = matches[0]
    assets = [item for item in snapshot.get("assets", [])
              if item.get("id") == proposal["asset_id"] and item.get("asset_type") == "table"]
    if len(assets) != 1:
        raise KnowledgeNotFound("Current metric table was not found")
    asset = assets[0]
    pointer = str(asset.get("source_pointer") or "").split("/")
    # Use native connector metadata only. Neither the caller nor imported prose
    # can choose a table or dialect through this planning endpoint.
    if source["kind"] == "postgres" and len(pointer) == 3 and pointer[0] == "postgres":
        dialect, schema, table = "postgres", pointer[1], pointer[2]
    elif source["kind"] == "sql_catalog" and len(pointer) == 4 and pointer[0] == "sql":
        dialect, schema, table = pointer[1:]
        if schema == "default":
            schema = None
    else:
        raise KnowledgeValidationError("Metric planning requires a supported native SQL table binding")
    if table != asset.get("name"):
        raise KnowledgeValidationError("Metric table identity disagrees with native metadata")
    metric = validate_metric_payload(proposal["payload"], asset.get("schema") or [])
    binding = {
        "connection_id": connection_id, "snapshot_id": snapshot["id"],
        "snapshot_version": snapshot["version"], "asset_id": asset["id"],
        "dialect": dialect, "table": {"name": table, **({"schema": schema} if schema else {})},
        "schema": asset.get("schema") or [], "metric_digest": metric_payload_digest(metric),
        "grain": asset.get("grain_contract"),
        "source_state": {"connection_status": source["status"], "agent_ready": True,
                         "last_error": source.get("last_error")},
        "approval": {"assertion_id": assertion_id, "status": "approved",
                     "effective_on_current_snapshot": True, "source_snapshot_id": snapshot["id"]},
    }
    result = plan_metric_query(binding, metric, request)
    # A dependency can lose access or gain a successor without changing this
    # source's snapshot ID. Recheck its effective meaning after plan assembly.
    observed = store.get_connection(tenant_id, connection_id)
    if (not observed or (observed.get("snapshot") or {}) != snapshot
            or any(observed.get(key) != source.get(key)
                   for key in ("kind", "status", "last_error", "config"))):
        raise KnowledgeConflict("Metric source changed during planning; reload current meaning")
    return result


def register_planning_tools(server: Any, store: Any, *, tenant_id: str = "demo") -> None:
    from knowledge_layer.cross_source import relationship_plan
    from knowledge_layer.mcp import READ_ONLY_ANNOTATIONS, _contract_tool_response
    from knowledge_layer.store import KnowledgeError

    def respond(call):
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            return _contract_tool_response(call())
        except KnowledgeError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Plan a current owner-reviewed metric from a published native SQL source. Returns parameterized SQL, schema/version pins and validation obligations. Executes no source query. Unsupported semantics or unavailable dependencies refuse.")
    def plan_metric_query(connection_id: str, assertion_id: str,
                          request: dict[str, Any]) -> dict[str, Any]:
        return respond(lambda: metric_plan(store, tenant_id=tenant_id, connection_id=connection_id,
                                           assertion_id=assertion_id, request=request))

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Inspect a published reviewed cross-source bridge, independently pinned to both current authorized sources. Fanout without result-grain policy blocks; coverage and uniqueness need independent source checks. Executes no query.")
    def plan_relationship(connection_id: str, assertion_id: str) -> dict[str, Any]:
        return respond(lambda: relationship_plan(store, tenant_id=tenant_id, connection_id=connection_id,
                                                  assertion_id=assertion_id))

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Read versioned product capabilities and their evidence boundaries. Availability, offline tests, live qualification and deployment are separate claims.")
    def get_capabilities() -> dict[str, Any]:
        from knowledge_layer.capabilities import capability_manifest
        return capability_manifest()

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Poll bounded authorized maintenance work after discovery. Shows version-bound interpretation packets, stale dependency impact, review/publication blockers and separate freshness clocks. No task means unknown when a capped scan is incomplete; no approval or publication is automated.")
    def get_maintenance_work(connection_id: str | None = None, cursor: str | None = None,
                             limit: int = 20) -> dict[str, Any]:
        from knowledge_layer.curation_handoff import maintenance_work
        return respond(lambda: maintenance_work(store, tenant_id=tenant_id, connection_id=connection_id,
                                                cursor=cursor, limit=limit))

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Resolve task context for explicitly selected sources: current semantics, metrics, company guidance, blockers and a short-lived principal-bound receipt. Optional inventory_selection freezes exact locally reviewed published concept IDs; inventory definitions establish meaning only. No source queries execute; revalidate before use.")
    def get_task_context(query: str, connection_ids: list[str], business_unit: str | None = None,
                         task: str | None = None,
                         inventory_selection: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from knowledge_layer.task_context import task_context
        return respond(lambda: task_context(store, tenant_id=tenant_id, query=query,
                                            connection_ids=connection_ids, business_unit=business_unit, task=task,
                                            inventory_selection=inventory_selection))

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Revalidate a task receipt against current permissions, source/semantic revisions and company guidance. Revoked, changed, expired and foreign-principal receipts refuse. Restart/replica continuity requires the operator's shared registry key ring. This grants no source execution permission.")
    def revalidate_task_context(receipt: dict[str, Any]) -> dict[str, Any]:
        from knowledge_layer.task_context import revalidate_context
        return respond(lambda: revalidate_context(store, tenant_id=tenant_id, receipt=receipt))
