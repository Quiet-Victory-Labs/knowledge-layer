"""Compact, read-only knowledge tools for external agent harnesses."""

from __future__ import annotations

import json
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import StrictBool

MAX_CONTRACT_RESPONSE_BYTES = 2 * 1024 * 1024

# Effect hints improve harness confirmation behavior; source authorization still applies.
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True,
)


class ContractResponseTooLarge(ValueError):
    """A complete machine contract cannot fit the response payload budget."""

    code = "contract_response_too_large"

    def __init__(self) -> None:
        super().__init__(
            f"{self.code}: Complete contract exceeds the {MAX_CONTRACT_RESPONSE_BYTES}-byte "
            "JSON response budget; narrow the source scope or its retained assertions."
        )


def _complete_contract_response(value: dict[str, Any]) -> dict[str, Any]:
    """Keep exact contracts or refuse; budget UTF-8 pretty JSON before the transport envelope.

    Pretty JSON also bounds the MCP SDK's text-content representation. Measuring
    incrementally avoids materializing another complete response just to count
    it. This does not assert source discovery or semantic completeness.
    """
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, indent=2)
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > MAX_CONTRACT_RESPONSE_BYTES:
            raise ContractResponseTooLarge
    return value


def _contract_tool_response(value: dict[str, Any]) -> dict[str, Any]:
    """Surface an expected budget refusal through the SDK's tool-error channel."""
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        return _complete_contract_response(value)
    except ContractResponseTooLarge as exc:
        raise ToolError(str(exc)) from exc


def _ontology_tool_response(store: Any, connection_id: str, version: int | None = None, *,
                            tenant_id: str = "demo") -> dict[str, Any]:
    """Export one authorized snapshot exactly; never clip serialized RDF."""
    from mcp.server.mcpserver.exceptions import ToolError

    from knowledge_layer.ontology import snapshot_turtle

    if version is not None and (type(version) is not int or version < 1):
        raise ValueError("version must be a positive integer")
    snapshot = store.get_snapshot(tenant_id, connection_id, version=version)
    if snapshot is None:
        raise ValueError("Snapshot not found")
    payload = {
        "connection_id": connection_id,
        "snapshot_id": snapshot.get("id") or snapshot.get("snapshot_id"),
        "snapshot_version": snapshot.get("version"),
        "media_type": "text/turtle",
        "turtle": snapshot_turtle(snapshot, tenant_id=tenant_id, connection_id=connection_id),
    }
    try:
        return _complete_contract_response(payload)
    except ContractResponseTooLarge as exc:
        raise ToolError(
            "ontology_response_too_large: Complete Turtle export exceeds the "
            f"{MAX_CONTRACT_RESPONSE_BYTES}-byte JSON response budget; narrow the source scope."
        ) from exc


def build_knowledge_mcp_server(
    store: Any, *, enable_source_management: bool = False,
    allowed_kinds: frozenset[str] = frozenset({"local_files"}),
    durable_jobs: bool = False, enable_autonomy: bool = False,
    tenant_id: str = "demo",
):
    """Expose the same versioned context through a small MCP surface.

    The host fixes the tenant at construction; tools cannot select another
    tenant. The enclosing transport supplies authentication and principal scope.
    """
    from mcp.server.mcpserver import MCPServer

    from knowledge_layer.store import validate_tenant_id

    tenant_id = validate_tenant_id(tenant_id)

    server = MCPServer(
        name="knowledge-layer",
        title="Knowledge Layer",
        version="0.1.0",
        description="Evidence-backed business context from connected customer sources.",
        instructions=(
            "Use these tools for compact source status and business context. "
            "Respect each assertion's trust state: inferred or candidate mappings are not certified. "
            "A missing or ambiguous definition is an unresolved result, not permission to guess. "
            "Respect tenant boundary assertions and partition pruning constraints on large analytical tables. "
            "These tools provide metadata and source bindings; they do not execute customer data queries."
            " Start with list_curation_work for current, bounded evidence tasks; submit only supported proposals."
            " Resolve current company skills/rules with resolve_guidance before company tasks. "
            "Blocked guidance requires resolving conflicts or unavailable dependencies."
        ),
    )

    from knowledge_layer.workspace_graph import register_workspace_tools

    register_workspace_tools(server, store, tenant_id=tenant_id)
    from knowledge_layer.build_transport import register_build_tools

    register_build_tools(server, store, tenant_id=tenant_id)

    @server.tool(name="list_knowledge_sources", description="List a bounded page of connected sources and curation states; pass next_cursor to continue.", annotations=READ_ONLY_ANNOTATIONS)
    def list_knowledge_sources(cursor: str | None = None, limit: int = 30) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be 1-50")
        page = store.page_connections(tenant_id, limit=limit, cursor=cursor)
        rows = page["connections"]
        jobs = {}
        if durable_jobs:
            from knowledge_layer.jobs import RefreshQueue

            jobs = RefreshQueue(store).active_for_connections(tenant_id, [row["id"] for row in rows])
        return {
            "connections": [
                _source_summary(row) | ({"refresh_job": jobs.get(row["id"])} if durable_jobs else {})
                for row in rows
            ],
            "total": page["total"],
            "next_cursor": page["next_cursor"],
            "tenant": tenant_id,
        }

    if enable_source_management:
        @server.tool(name="get_refresh_schedule", description="Read recurring source refresh configuration and actual completed-discovery freshness.", annotations=READ_ONLY_ANNOTATIONS)
        def get_refresh_schedule(connection_id: str) -> dict[str, Any]:
            from knowledge_layer.source_onboarding import source_schedule

            return source_schedule(store, tenant_id=tenant_id, allowed_kinds=allowed_kinds,
                                   connection_id=connection_id)

        @server.tool(name="set_refresh_schedule", description="Set or pause hourly, daily, weekly or monthly refreshes with an IANA timezone; existing worker and direct source manage grant required.")
        def set_refresh_schedule(connection_id: str, config: dict[str, Any], paused: StrictBool = False) -> dict[str, Any]:
            from knowledge_layer.source_onboarding import source_schedule

            return source_schedule(store, tenant_id=tenant_id, allowed_kinds=allowed_kinds,
                                   connection_id=connection_id, config=config, paused=paused)

        @server.tool(name="list_connectable_source_types", description="List operator-enabled source kinds, scopes, and safe connection fields.", annotations=READ_ONLY_ANNOTATIONS)
        def list_connectable_source_types() -> dict[str, Any]:
            from knowledge_layer.source_onboarding import connector_options

            return {"connector_types": connector_options(store, allowed_kinds)}

        @server.tool(name="connect_knowledge_source", description="Connect and discover one operator-enabled source using safe scope settings, never credentials.")
        def connect_knowledge_source(name: str, kind: str, config: dict[str, Any]) -> dict[str, Any]:
            from knowledge_layer.source_onboarding import connect_source
            from knowledge_layer.store import KnowledgeError

            try:
                return _bound(connect_source(
                    store, tenant_id=tenant_id, allowed_kinds=allowed_kinds,
                    name=name, kind=kind, config=config, durable_jobs=durable_jobs,
                ))
            except KnowledgeError as exc:
                from mcp.server.mcpserver.exceptions import ToolError
                raise ToolError(str(exc)) from exc

        @server.tool(name="refresh_knowledge_source", description="Reprofile changed assets for one already connected, operator-enabled source.")
        def refresh_knowledge_source(connection_id: str) -> dict[str, Any]:
            from knowledge_layer.source_onboarding import refresh_source
            from knowledge_layer.store import KnowledgeError

            try:
                return _bound(refresh_source(
                    store, tenant_id=tenant_id, allowed_kinds=allowed_kinds,
                    connection_id=connection_id, durable_jobs=durable_jobs,
                ))
            except KnowledgeError as exc:
                from mcp.server.mcpserver.exceptions import ToolError
                raise ToolError(str(exc)) from exc

    if durable_jobs:
        @server.tool(name="get_refresh_job", description="Read a queued source refresh job; access requires a source grant.", annotations=READ_ONLY_ANNOTATIONS)
        def get_refresh_job(job_id: str) -> dict[str, Any]:
            from knowledge_layer.jobs import RefreshQueue

            job = RefreshQueue(store).get(tenant_id, job_id)
            return {"job": job} if job else {"status": "not_found"}

        if enable_source_management:
            @server.tool(name="cancel_refresh_job", description="Request cancellation of a queued or running refresh you manage.")
            def cancel_refresh_job(job_id: str) -> dict[str, Any]:
                from knowledge_layer.jobs import RefreshQueue

                return {"job": RefreshQueue(store).cancel(tenant_id, job_id)}

    @server.tool(name="get_curation_status", description="Get one source's curation progress and unresolved count.", annotations=READ_ONLY_ANNOTATIONS)
    def get_curation_status(connection_id: str) -> dict[str, Any]:
        row = store.get_connection(tenant_id, connection_id)
        if row is None:
            return {"status": "not_found", "connection_id": connection_id}
        result = _source_summary(row)
        if durable_jobs:
            from knowledge_layer.jobs import RefreshQueue

            result["refresh_job"] = RefreshQueue(store).active_for_connections(
                tenant_id, [connection_id]
            ).get(connection_id)
        return result

    @server.tool(name="list_curation_work", description="Get small, prioritized evidence packets for model-assisted ontology curation after source changes.", annotations=READ_ONLY_ANNOTATIONS)
    def list_curation_work_tool(connection_id: str | None = None, limit: int = 3, offset: int = 0) -> dict[str, Any]:
        from knowledge_layer.work import list_curation_work

        return _bound(list_curation_work(store, tenant_id=tenant_id, connection_id=connection_id, limit=limit, offset=offset))

    @server.tool(name="list_contract_work", description="Get bounded table, column, grain, join, and metric curation packets with document evidence.", annotations=READ_ONLY_ANNOTATIONS)
    def list_contract_work_tool(connection_id: str, limit: int = 5, offset: int = 0) -> dict[str, Any]:
        from knowledge_layer.contracts import list_contract_work

        return _bound(list_contract_work(store, tenant_id=tenant_id, connection_id=connection_id,
                                         limit=limit, offset=offset))

    @server.tool(name="search_source_evidence", description="Find bounded source metadata and document excerpts by business term within one connection.", annotations=READ_ONLY_ANNOTATIONS)
    def search_source_evidence_tool(connection_id: str, query: str, limit: int = 5) -> dict[str, Any]:
        from knowledge_layer.contracts import search_source_evidence

        return _bound(search_source_evidence(store, tenant_id=tenant_id, connection_id=connection_id,
                                             query=query, limit=limit))

    @server.tool(name="search_document_evidence", description="Search bounded current document excerpts across connected sources; returns snapshot-pinned citation refs.", annotations=READ_ONLY_ANNOTATIONS)
    def search_document_evidence_tool(query: str, limit: int = 5) -> dict[str, Any]:
        from knowledge_layer.contracts import search_document_evidence

        return _bound(search_document_evidence(store, tenant_id=tenant_id, query=query, limit=limit))

    @server.tool(name="get_semantic_contract", description="Read source bindings, glossary, approved contract assertions, and pending drafts with trust state.", annotations=READ_ONLY_ANNOTATIONS)
    def get_semantic_contract_tool(connection_id: str) -> dict[str, Any]:
        from knowledge_layer.contracts import semantic_contract

        return _contract_tool_response(semantic_contract(store, tenant_id=tenant_id, connection_id=connection_id))

    @server.tool(name="get_ontology_export", description="Export complete authorized RDF/Turtle. Omit version for current effective review and publication readiness; an explicit version shows decisions as of snapshot creation without current publication state. Oversized exports refuse rather than truncate.", annotations=READ_ONLY_ANNOTATIONS)
    def get_ontology_export(connection_id: str, version: int | None = None) -> dict[str, Any]:
        return _ontology_tool_response(store, connection_id, version, tenant_id=tenant_id)

    @server.tool(name="get_ossie_preview", description="Get a schema-validated Apache Ossie draft for SQL sources; never indicates certification or query validation.", annotations=READ_ONLY_ANNOTATIONS)
    def get_ossie_preview_tool(connection_id: str) -> dict[str, Any]:
        from knowledge_layer.contracts import ossie_preview

        return _contract_tool_response(ossie_preview(store, tenant_id=tenant_id, connection_id=connection_id))

    @server.tool(name="propose_contract_assertion", description="Propose evidence-bound dataset/field description, grain, join rule, metric, SQL example, or tenant boundary; always requires owner review.")
    def propose_contract_assertion_tool(
        connection_id: str, assertion_kind: str, asset_id: str, payload: dict[str, Any],
        evidence_ids: list[str], rationale: str, expected_snapshot_id: str,
        evidence_refs: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        from knowledge_layer.store import KnowledgeError

        try:
            proposal = store.submit_contract_assertion(
                tenant_id, connection_id, assertion_kind=assertion_kind, asset_id=asset_id,
                payload=payload, evidence_ids=evidence_ids, rationale=rationale,
                expected_snapshot_id=expected_snapshot_id, evidence_refs=evidence_refs,
            )
        except KnowledgeError as exc:
            from mcp.server.mcpserver.exceptions import ToolError
            raise ToolError(str(exc)) from exc
        return {"proposal": _bound(proposal), "approval_required": True}

    @server.tool(name="resolve_business_term", description="Find concepts, approved metrics and cited glossary; token paraphrases are labeled suggestions requiring scope confirmation.", annotations=READ_ONLY_ANNOTATIONS)
    def resolve_business_term(term: str, connection_id: str | None = None) -> dict[str, Any]:
        if not term.strip() or len(term) > 120:
            raise ValueError("term must be between 1 and 120 characters")
        context = store.get_context(tenant_id, term=term.strip(), connection_id=connection_id)
        if context is None or not any(context.get(key) for key in ("matches", "cited_glossary", "cited_upstream_definitions", "metric_matches", "retrieval_candidates")):
            return {"status": "not_found", "term": term.strip()}
        return _contract_tool_response(_compact_term_context(context))

    @server.tool(name="get_concept_context", description="Read one concept's definition, trust, evidence, and relationships.", annotations=READ_ONLY_ANNOTATIONS)
    def get_concept_context(concept_id: str) -> dict[str, Any]:
        context = store.get_context(tenant_id, concept_id=concept_id)
        if context is None:
            return {"status": "not_found", "concept_id": concept_id}
        if context.get("basis") == "inventory":
            return _contract_tool_response(context)
        return _bound(context)

    @server.tool(name="search_knowledge", description="Find relevant business concepts with bounded hybrid semantic and lexical retrieval. Published inventory definitions retain exact scope and local publication pins; relevance alone does not approve equivalence or source execution.")
    def search_knowledge_tool(query: str, limit: int = 5) -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        from knowledge_layer.retrieval import search_knowledge
        from knowledge_layer.store import KnowledgeError

        try:
            result = search_knowledge(store, tenant_id=tenant_id, query=query, limit=limit)
            if any(item.get("basis") == "inventory" for item in result["results"]):
                return _contract_tool_response(result)
            return _bound(result)
        except KnowledgeError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="get_relationship_neighborhood", description="Read a bounded, evidence-backed graph neighborhood around one concept or asset.", annotations=READ_ONLY_ANNOTATIONS)
    def get_relationship_neighborhood(connection_id: str, focus_id: str, depth: int = 1, limit: int = 30) -> dict[str, Any]:
        from knowledge_layer.store import KnowledgeError

        if not 0 <= depth <= 2 or not 1 <= limit <= 60:
            raise ValueError("depth must be 0-2 and limit must be 1-60")
        try:
            return _compact_graph(store.get_graph(tenant_id, connection_id, focus_id=focus_id, depth=depth, limit=limit))
        except KnowledgeError as exc:
            from mcp.server.mcpserver.exceptions import ToolError
            raise ToolError(str(exc)) from exc

    @server.tool(name="get_federated_evidence_graph", description="Trace current table or field assertions to cited document evidence and glossary nodes across connections.", annotations=READ_ONLY_ANNOTATIONS)
    def get_federated_evidence_graph(connection_id: str, limit: int = 3) -> dict[str, Any]:
        from knowledge_layer.federated import federated_evidence_graph
        from knowledge_layer.store import KnowledgeError

        try:
            return _compact_federated_graph(federated_evidence_graph(
                store, tenant_id=tenant_id, connection_id=connection_id, limit=limit,
            ))
        except KnowledgeError as exc:
            from mcp.server.mcpserver.exceptions import ToolError
            raise ToolError(str(exc)) from exc

    @server.tool(name="propose_semantic_change", description="Submit an evidence-backed definition or mapping correction for review; never certifies it.")
    def propose_semantic_change(
        connection_id: str,
        target_concept_id: str,
        rationale: str,
        evidence_ids: list[str],
        proposed_definition: str | None = None,
        proposed_mapping: dict[str, Any] | None = None,
        expected_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        from knowledge_layer.store import KnowledgeError

        try:
            proposal = store.submit_proposal(
                tenant_id,
                connection_id,
                target_concept_id=target_concept_id,
                proposed_definition=proposed_definition,
                proposed_mapping=proposed_mapping,
                evidence_ids=evidence_ids,
                rationale=rationale,
                expected_snapshot_id=expected_snapshot_id,
            )
        except KnowledgeError as exc:
            from mcp.server.mcpserver.exceptions import ToolError
            raise ToolError(str(exc)) from exc
        return {"proposal": _bound(proposal), "approval_required": True}

    from knowledge_layer.guidance_mcp import register_guidance_tools

    register_guidance_tools(server, store, tenant_id=tenant_id)
    from knowledge_layer.planning import register_planning_tools

    register_planning_tools(server, store, tenant_id=tenant_id)
    from knowledge_layer.inventory_transport import register_inventory_tools

    register_inventory_tools(server, store, allow_writes=enable_source_management,
                             allowed_kinds=allowed_kinds, tenant_id=tenant_id)
    from knowledge_layer.autonomy_transport import register_autonomy_tools

    register_autonomy_tools(server, store, enable_autonomy=enable_autonomy, tenant_id=tenant_id)
    return server


def _source_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Do not expose connection configuration or any future secret fields."""
    allowed = (
        "id", "name", "kind", "connector_type", "status", "stage", "discovery_mode", "last_synced_at",
        "last_refresh_attempt_at", "last_complete_refresh_at",
        "asset_count", "concept_count", "relationship_count", "proposal_count",
        "needs_attention_count", "stale_assertion_count", "coverage", "error", "created_at", "updated_at",
    )
    summary = {key: row[key] for key in allowed if key in row}
    snapshot = row.get("snapshot") or {}
    summary.update({
        "discovery_complete": None if row.get("discovery_mode") == "inventory" else snapshot.get("discovery_complete") is True,
        "meaning_certified": snapshot.get("meaning_certified") is True,
        "agent_ready": snapshot.get("agent_ready") is True,
        "curation_status": snapshot.get("curation_status"),
        "snapshot_id": snapshot.get("id"),
        "snapshot_version": snapshot.get("version"),
    })
    return summary


def _compact_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Keep agent graph responses small and internally referentially complete."""
    nodes = graph.get("nodes") or []
    shown = nodes[:20]
    ids = {node["id"] for node in shown}
    edges = [edge for edge in graph.get("edges") or [] if edge.get("subject_id") in ids and edge.get("object_id") in ids]
    return {
        "connection_id": graph.get("connection_id"),
        "version": graph.get("version"),
        "focus_id": graph.get("focus_id"),
        "nodes": [
            {**{key: node.get(key) for key in ("id", "kind", "label", "status", "trust_state")},
             "description": (node.get("description") or "")[:240],
             "evidence_ids": (node.get("evidence_ids") or [])[:3]}
            for node in shown
        ],
        "edges": [
            {key: edge.get(key) for key in ("id", "subject_id", "predicate", "object_id", "status", "trust_state", "evidence_ids")}
            for edge in edges[:20]
        ],
        "truncated": bool(graph.get("truncated") or len(nodes) > 20 or len(edges) > 20),
    }


def _compact_federated_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Preserve source and document provenance within a small agent response."""
    nodes = (graph.get("nodes") or [])[:36]
    ids = {node["id"] for node in nodes}
    edges = [edge for edge in graph.get("edges") or []
             if edge.get("subject_id") in ids and edge.get("object_id") in ids][:48]
    return {
        "connection_id": graph.get("connection_id"), "snapshot_id": graph.get("snapshot_id"),
        "version": graph.get("version"), "assertion_count": graph.get("assertion_count"),
        "linked_assertions": graph.get("linked_assertions"),
        "stale_citations": graph.get("stale_citations"),
        "nodes": [{key: node[key] for key in (
            "id", "connection_id", "snapshot_id", "kind", "label", "status", "trust_state",
            "inventory_id", "manifest_digest", "record_id", "record_kind", "record_digest", "evidence_ref",
            "source_text_authority", "native_authority", "meaning_certified", "locally_executable") if key in node}
            | {"description": (node.get("description") or "")[:240]}
            for node in nodes],
        "edges": [{key: edge.get(key) for key in (
            "id", "subject_id", "predicate", "object_id", "status")}
            for edge in edges],
        "truncated": bool(graph.get("truncated") or len(graph.get("nodes") or []) > len(nodes)
                          or len(graph.get("edges") or []) > len(edges)),
        "warning": graph.get("warning"),
    }


def _compact_term_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep broad term lookups below a useful agent context budget."""
    matches = context.get("matches") or []
    compact = []
    for match in matches[:3]:
        if match.get("basis") == "inventory":
            # Authority pins and a reviewed definition form one indivisible packet.
            compact.append(match)
            continue
        concept = match.get("concept") or {}
        relationships = match.get("relationships") or []
        compact.append({
            "concept": {
                key: concept.get(key)
                for key in ("id", "name", "semantic_type", "status", "trust_state", "confidence", "definition", "definition_status", "upstream_definition")
                if concept.get(key) is not None
            } | (
                {"candidate_definitions": concept["candidate_definitions"][:3]}
                if concept.get("candidate_definitions") else {}
            ),
            "trust_state": match.get("trust_state"),
            "snapshot": match.get("snapshot"),
            "evidence_ids": (match.get("evidence_ids") or [])[:5],
            "evidence": [
                {key: item.get(key) for key in ("id", "summary", "source_pointer") if item.get(key) is not None}
                for item in (match.get("evidence") or [])[:2]
            ],
            "source_bindings": [
                {key: item.get(key) for key in ("connection_id", "connection_name", "kind", "connection_status", "last_error", "last_refresh_attempt_at", "last_complete_refresh_at") if item.get(key) is not None}
                for item in (match.get("source_bindings") or [])[:1]
            ],
            "relationship_count": len(relationships),
            "relationships": [
                {key: item.get(key) for key in ("id", "predicate", "subject_id", "object_id", "status", "ambiguity") if item.get(key) is not None}
                for item in relationships[:2]
            ],
            "cross_source_candidates": [
                {key: item.get(key) for key in ("concept_id", "name", "connection_id", "status", "certified") if item.get(key) is not None}
                for item in (match.get("cross_source_candidates") or [])[:3]
            ],
        })
    glossary_fields = (
        "concept_id", "term", "definition", "definition_status", "trust_state",
        "concept_trust_state", "connection_id", "snapshot_id", "snapshot_version",
        "evidence_ref", "evidence_ids", "source_pointer", "source_assertion",
    )
    metric_fields = (
        "id", "name", "description", "field", "aggregation", "unit", "status", "trust_state",
        "certified", "evidence_ids", "decision_proposal_id", "source_asset_id", "connection_id",
        "source_kind", "snapshot_id", "snapshot_version", "source_pointer", "asset_name", "contract_ref",
        "query_guidance", "match_basis", "score", "result_kind",
    )
    upstream_fields = glossary_fields + (
        "definition_proposal_id", "source_revision", "upstream_definition", "executable", "certified",
    )
    candidates = []
    for candidate in (context.get("retrieval_candidates") or [])[:3]:
        detail = candidate.get("context") or {}
        kind = candidate.get("candidate_type")
        if detail.get("basis") == "inventory":
            pass
        elif kind == "concept":
            detail = _compact_term_context({"matches": [detail]})["matches"][0]
        else:
            fields = metric_fields if kind == "metric" else upstream_fields if kind == "cited_upstream_definition" else glossary_fields
            detail = {key: detail[key] for key in fields if key in detail}
        candidates.append({key: candidate[key] for key in (
            "candidate_type", "match_basis", "score", "result_kind", "matched_tokens", "requires_confirmation",
        ) if key in candidate} | {"context": detail})
    return {
        **context,
        "matches": compact,
        "total_matches": context.get("total_matches", len(matches)),
        "more_available": bool(context.get("more_available") or len(matches) > 3),
        "metric_matches": [{key: item[key] for key in metric_fields if key in item}
                           for item in (context.get("metric_matches") or [])[:3]],
        "metric_total_matches": context.get("metric_total_matches", len(context.get("metric_matches") or [])),
        "metric_more_available": bool(context.get("metric_more_available") or len(context.get("metric_matches") or []) > 3),
        "retrieval_candidates": candidates,
        "retrieval_candidate_total_matches": context.get("retrieval_candidate_total_matches", len(context.get("retrieval_candidates") or [])),
        "retrieval_candidate_more_available": bool(context.get("retrieval_candidate_more_available") or len(context.get("retrieval_candidates") or []) > 3),
        "cited_glossary": [
            {key: item[key] for key in glossary_fields if key in item}
            for item in (context.get("cited_glossary") or [])[:3]
        ],
        "cited_glossary_total_matches": context.get(
            "cited_glossary_total_matches", len(context.get("cited_glossary") or [])),
        "cited_glossary_more_available": bool(
            context.get("cited_glossary_more_available") or len(context.get("cited_glossary") or []) > 3),
        "cited_upstream_definitions": [{key: item[key] for key in upstream_fields if key in item}
                                       for item in (context.get("cited_upstream_definitions") or [])[:3]],
        "cited_upstream_definitions_total_matches": context.get(
            "cited_upstream_definitions_total_matches", len(context.get("cited_upstream_definitions") or [])),
        "cited_upstream_definitions_more_available": bool(
            context.get("cited_upstream_definitions_more_available") or len(context.get("cited_upstream_definitions") or []) > 3),
    }


def _bound(value: Any, depth: int = 0) -> Any:
    """Keep tool responses useful without dumping entire schemas or documents."""
    if depth > 5 and isinstance(value, (dict, list)):
        return "[more detail omitted]"
    if isinstance(value, dict):
        return {str(key): _bound(item, depth + 1) for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        return [_bound(item, depth + 1) for item in value[:20]]
    if isinstance(value, str) and len(value) > 1200:
        return value[:1200] + "…"
    return value
