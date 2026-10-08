"""Small HTTP equivalent of the knowledge MCP tools for other agent clients."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from knowledge_layer.contracts import (
    list_contract_work,
    ossie_preview,
    search_document_evidence,
    search_source_evidence,
    semantic_contract,
)
from knowledge_layer.federated import federated_evidence_graph
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.mcp import (
    MAX_CONTRACT_RESPONSE_BYTES,
    ContractResponseTooLarge,
    _bound,
    _compact_federated_graph,
    _compact_graph,
    _compact_term_context,
    _complete_contract_response,
    _source_summary,
)
from knowledge_layer.ontology import snapshot_turtle
from knowledge_layer.retrieval import search_knowledge
from knowledge_layer.source_onboarding import (
    connect_source,
    connector_options,
    refresh_source,
    source_schedule,
)
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
)
from knowledge_layer.work import list_curation_work


class AgentProposal(BaseModel):
    connection_id: str = Field(min_length=1, max_length=160)
    target_concept_id: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=8, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    proposed_definition: str | None = Field(default=None, max_length=4000)
    proposed_mapping: dict[str, Any] | None = None
    expected_snapshot_id: str | None = Field(default=None, min_length=1, max_length=160)


class AgentSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)


class AgentRefreshSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    paused: StrictBool = False


class AgentContractAssertion(BaseModel):
    connection_id: str = Field(min_length=1, max_length=160)
    assertion_kind: str = Field(min_length=1, max_length=40)
    asset_id: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any]
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    evidence_refs: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=8, max_length=1000)
    expected_snapshot_id: str = Field(min_length=1, max_length=128)


def build_agent_api(
    store: Any, *, enable_source_management: bool = False,
    allowed_kinds: frozenset[str] = frozenset({"local_files"}),
    durable_jobs: bool = False, enable_autonomy: bool = False,
) -> FastAPI:
    app = FastAPI(
        title="Knowledge Layer Agent API",
        description="Evidence-backed context and reviewable curation proposals for external agents.",
        version="0.1.0",
    )

    from knowledge_layer.build_transport import create_build_router

    app.include_router(create_build_router(store, agent=True))

    @app.get("/build-handoff")
    def get_build_handoff(
        connection_id: str | None = Query(default=None, min_length=1, max_length=64),
        cursor: str | None = Query(default=None, min_length=1, max_length=2000),
        limit: int = Query(default=20, ge=1, le=20),
    ) -> dict[str, Any]:
        from knowledge_layer.build_handoff import build_handoff

        try:
            return _complete_contract_response(build_handoff(
                store, tenant_id="demo", connection_id=connection_id, cursor=cursor, limit=limit,
            ))
        except KnowledgeError as exc:
            code = 404 if isinstance(exc, KnowledgeNotFound) else 403 if isinstance(exc, KnowledgeAccessDenied) else 409 if isinstance(exc, KnowledgeConflict) else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail={
                "code": exc.code, "message": str(exc), "max_response_bytes": MAX_CONTRACT_RESPONSE_BYTES,
            }) from exc

    @app.get("/workspace/graph")
    @app.get("/workspace/ontology")
    def collective_graph(
        request: Request,
        connection_id: str | None = Query(default=None, max_length=160),
        q: str | None = Query(default=None, max_length=200),
        focus_id: str | None = Query(default=None, min_length=1, max_length=160),
        depth: int = Query(default=2, ge=0, le=3),
        limit: int = Query(default=180, ge=10, le=400),
        source_limit: int = Query(default=40, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=512),
    ) -> dict[str, Any]:
        from knowledge_layer.workspace_graph import workspace_graph, workspace_ontology

        try:
            value = workspace_graph(store, tenant_id="demo", connection_id=connection_id, q=q,
                                    focus_id=focus_id, depth=depth, limit=limit,
                                    source_limit=source_limit, cursor=cursor)
            return _complete_contract_response(workspace_ontology(value) if request.url.path.endswith("/ontology") else value)
        except KnowledgeError as exc:
            code = 404 if isinstance(exc, KnowledgeNotFound) else 403 if isinstance(exc, KnowledgeAccessDenied) else 409 if isinstance(exc, KnowledgeConflict) else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
    queue = RefreshQueue(store) if durable_jobs else None

    from knowledge_layer.guidance_api import create_guidance_router

    app.include_router(create_guidance_router(store))
    from knowledge_layer.consumer_api import create_consumer_router

    app.include_router(create_consumer_router(store))
    from knowledge_layer.inventory_transport import create_inventory_router

    app.include_router(create_inventory_router(store, allow_writes=enable_source_management, allowed_kinds=allowed_kinds))
    from knowledge_layer.autonomy_transport import create_autonomy_router

    app.include_router(create_autonomy_router(store, enable_autonomy=enable_autonomy))

    @app.get("/sources")
    def sources(
        limit: int = Query(default=30, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=512),
    ) -> dict[str, Any]:
        try:
            page = store.page_connections("demo", limit=limit, cursor=cursor)
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        rows = page["connections"]
        jobs = queue.active_for_connections("demo", [row["id"] for row in rows]) if queue else {}
        return {"connections": [
            _source_summary(row) | ({"refresh_job": jobs.get(row["id"])} if queue else {})
            for row in rows
        ], "total": page["total"], "next_cursor": page["next_cursor"]}

    if enable_source_management:
        @app.get("/sources/{connection_id}/refresh-schedule")
        def get_source_schedule(connection_id: str) -> dict[str, Any]:
            try:
                return source_schedule(store, tenant_id="demo", allowed_kinds=allowed_kinds,
                                       connection_id=connection_id)
            except KnowledgeError as exc:
                code = 404 if isinstance(exc, KnowledgeNotFound) else 403 if isinstance(exc, KnowledgeAccessDenied) else 422
                raise HTTPException(status_code=code, detail=str(exc)) from exc

        @app.put("/sources/{connection_id}/refresh-schedule")
        def set_source_schedule(connection_id: str, body: AgentRefreshSchedule) -> dict[str, Any]:
            try:
                return source_schedule(store, tenant_id="demo", allowed_kinds=allowed_kinds,
                                       connection_id=connection_id, **body.model_dump())
            except KnowledgeError as exc:
                code = 404 if isinstance(exc, KnowledgeNotFound) else 403 if isinstance(exc, KnowledgeAccessDenied) else 422
                raise HTTPException(status_code=code, detail=str(exc)) from exc

        @app.get("/connector-types")
        def connectable_types() -> dict[str, Any]:
            return {"connector_types": connector_options(store, allowed_kinds)}

        @app.post("/sources", status_code=201)
        def create_source(body: AgentSourceCreate) -> dict[str, Any]:
            try:
                return connect_source(
                    store, tenant_id="demo", allowed_kinds=allowed_kinds,
                    durable_jobs=durable_jobs, **body.model_dump()
                )
            except KnowledgeError as exc:
                raise HTTPException(status_code=403 if isinstance(exc, KnowledgeAccessDenied) else 422,
                                    detail=str(exc)) from exc

        @app.post("/sources/{connection_id}/refresh")
        def refresh_existing_source(connection_id: str) -> dict[str, Any]:
            try:
                return refresh_source(
                    store, tenant_id="demo", allowed_kinds=allowed_kinds, connection_id=connection_id,
                    durable_jobs=durable_jobs,
                )
            except KnowledgeError as exc:
                code = (404 if isinstance(exc, KnowledgeNotFound) else
                        403 if isinstance(exc, KnowledgeAccessDenied) else 422)
                raise HTTPException(status_code=code, detail=str(exc)) from exc

    if queue is not None:
        @app.get("/jobs/{job_id}")
        def get_refresh_job(job_id: str) -> dict[str, Any]:
            job = queue.get("demo", job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Refresh job not found")
            return {"job": job}

        if enable_source_management:
            @app.post("/jobs/{job_id}/cancel")
            def cancel_refresh_job(job_id: str) -> dict[str, Any]:
                try:
                    return {"job": queue.cancel("demo", job_id)}
                except KnowledgeError as exc:
                    code = 404 if isinstance(exc, KnowledgeNotFound) else 403
                    raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.get("/sources/{connection_id}/status")
    def status(connection_id: str) -> dict[str, Any]:
        row = store.get_connection("demo", connection_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        return _source_summary(row) | ({
            "refresh_job": queue.active_for_connections("demo", [connection_id]).get(connection_id)
        } if queue else {})

    @app.get("/curation-work")
    def curation_work(
        connection_id: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=3, ge=1, le=10),
        offset: int = Query(default=0, ge=0, lt=500),
    ) -> dict[str, Any]:
        try:
            return _bound(list_curation_work(store, tenant_id="demo", connection_id=connection_id, limit=limit, offset=offset))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/contract-work")
    def contract_work(
        connection_id: str = Query(min_length=1, max_length=64),
        limit: int = Query(default=5, ge=1, le=10),
        offset: int = Query(default=0, ge=0, lt=500),
    ) -> dict[str, Any]:
        try:
            return _bound(list_contract_work(store, tenant_id="demo", connection_id=connection_id,
                                             limit=limit, offset=offset))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/sources/{connection_id}/contract")
    def contract(connection_id: str) -> dict[str, Any]:
        try:
            return _complete_contract_response(semantic_contract(store, tenant_id="demo", connection_id=connection_id))
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail={
                "code": exc.code, "message": str(exc), "max_response_bytes": MAX_CONTRACT_RESPONSE_BYTES,
            }) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/sources/{connection_id}/ossie-preview")
    def ossie_contract_preview(connection_id: str) -> dict[str, Any]:
        try:
            return _complete_contract_response(ossie_preview(store, tenant_id="demo", connection_id=connection_id))
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail={
                "code": exc.code, "message": str(exc), "max_response_bytes": MAX_CONTRACT_RESPONSE_BYTES,
            }) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/sources/{connection_id}/evidence/search")
    def evidence_search(connection_id: str, q: str = Query(min_length=2, max_length=120),
                        limit: int = Query(default=5, ge=1, le=10)) -> dict[str, Any]:
        try:
            return _bound(search_source_evidence(store, tenant_id="demo", connection_id=connection_id,
                                                 query=q, limit=limit))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/document-evidence/search")
    def document_evidence_search(q: str = Query(min_length=2, max_length=120),
                                 limit: int = Query(default=5, ge=1, le=10)) -> dict[str, Any]:
        return _bound(search_document_evidence(store, tenant_id="demo", query=q, limit=limit))

    @app.get("/sources/{connection_id}/ontology.ttl")
    def ontology(
        connection_id: str,
        version: int | None = Query(default=None, ge=1),
    ) -> Response:
        snapshot = store.get_snapshot("demo", connection_id, version=version)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return Response(
            snapshot_turtle(snapshot, tenant_id="demo", connection_id=connection_id),
            media_type="text/turtle",
        )

    @app.get("/context")
    def context(
        term: str | None = Query(default=None, min_length=1, max_length=120),
        concept_id: str | None = Query(default=None, min_length=1, max_length=160),
        connection_id: str | None = Query(default=None, min_length=1, max_length=64),
    ) -> dict[str, Any]:
        if bool(term) == bool(concept_id):
            raise HTTPException(status_code=422, detail="Pass exactly one of term or concept_id")
        try:
            result = store.get_context("demo", concept_id=concept_id, term=term, connection_id=connection_id)
        except KnowledgeError as exc:
            code = 404 if isinstance(exc, KnowledgeNotFound) else 403 if isinstance(exc, KnowledgeAccessDenied) else 409 if isinstance(exc, KnowledgeConflict) else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        if result is None or (term and not any(result.get(key) for key in ("matches", "cited_glossary", "cited_upstream_definitions", "metric_matches", "retrieval_candidates"))):
            raise HTTPException(status_code=404, detail="Concept not found")
        try:
            if term:
                return _complete_contract_response(_compact_term_context(result))
            return _complete_contract_response(result) if result.get("basis") == "inventory" else _bound(result)
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @app.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=5, ge=1, le=10),
    ) -> dict[str, Any]:
        try:
            result = search_knowledge(store, tenant_id="demo", query=q, limit=limit)
            return (_complete_contract_response(result)
                    if any(item.get("basis") == "inventory" for item in result["results"]) else _bound(result))
        except KnowledgeError as exc:
            code = 404 if isinstance(exc, KnowledgeNotFound) else 403 if isinstance(exc, KnowledgeAccessDenied) else 409 if isinstance(exc, KnowledgeConflict) else 422
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @app.get("/graph")
    def graph(
        connection_id: str = Query(min_length=1, max_length=64),
        focus_id: str = Query(min_length=1, max_length=160),
        depth: int = Query(default=1, ge=0, le=2),
        limit: int = Query(default=30, ge=1, le=60),
        version: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        try:
            return _compact_graph(store.get_graph("demo", connection_id, focus_id=focus_id, depth=depth, limit=limit, version=version))
        except KnowledgeError as exc:
            raise HTTPException(status_code=404 if isinstance(exc, KnowledgeNotFound) else 422, detail=str(exc)) from exc

    @app.get("/federated-graph")
    def federated_graph(
        connection_id: str = Query(min_length=1, max_length=64),
        limit: int = Query(default=3, ge=1, le=8),
    ) -> dict[str, Any]:
        try:
            return _compact_federated_graph(federated_evidence_graph(
                store, tenant_id="demo", connection_id=connection_id, limit=limit,
            ))
        except KnowledgeError as exc:
            raise HTTPException(status_code=404 if isinstance(exc, KnowledgeNotFound) else 422, detail=str(exc)) from exc

    @app.post("/proposals", status_code=202)
    def propose_change(body: AgentProposal) -> dict[str, Any]:
        try:
            proposal = store.submit_proposal("demo", **body.model_dump())
        except KnowledgeError as exc:
            raise HTTPException(status_code=409 if isinstance(exc, KnowledgeConflict) else 422, detail=str(exc)) from exc
        return {"proposal": _bound(proposal), "approval_required": True}

    @app.post("/contract-assertions", status_code=202)
    def propose_contract(body: AgentContractAssertion) -> dict[str, Any]:
        try:
            proposal = store.submit_contract_assertion("demo", **body.model_dump())
        except KnowledgeError as exc:
            raise HTTPException(status_code=409 if isinstance(exc, KnowledgeConflict) else 422,
                                detail=str(exc)) from exc
        return {"proposal": _bound(proposal), "approval_required": True}

    return app
