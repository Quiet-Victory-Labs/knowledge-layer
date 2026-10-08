"""HTTP routes for tenant-scoped source connections and curated knowledge."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from knowledge_layer.federated import federated_evidence_graph
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.schedules import RefreshSchedules
from knowledge_layer.store import (
    CurationFailed,
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


class ConnectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectionConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]


class RefreshScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    paused: StrictBool = False


class SemanticProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_concept_id: str = Field(min_length=1, max_length=160)
    proposed_definition: str | None = Field(default=None, max_length=4000)
    proposed_mapping: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=8, max_length=1000)
    proposal_type: str = Field(default="semantic_correction", min_length=2, max_length=64)
    expected_snapshot_id: str | None = Field(default=None, min_length=1, max_length=160)


class ProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    rationale: str | None = Field(default=None, max_length=1000)


class ProposalArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=8, max_length=1000)


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_snapshot_id: str = Field(min_length=1, max_length=128)
    expected_snapshot_version: int = Field(ge=1)
    rationale: str = Field(min_length=16, max_length=1000)


class PublicationWithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=1000)


class ReviewBatchPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_ids: list[str] = Field(min_length=1, max_length=100)
    expected_snapshot_id: str = Field(min_length=1, max_length=128)
    expected_snapshot_version: StrictInt = Field(ge=1)


class ReviewBatchAcceptanceRequest(ReviewBatchPreviewRequest):
    expected_review_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=16, max_length=1000)
    publish: StrictBool = False


def create_knowledge_router(
    *, store: KnowledgeStore, allowed_kinds: frozenset[str] | None = None,
    durable_jobs: bool = False, enable_autonomy: bool = False,
) -> APIRouter:
    """Create the local platform routes around an injected persistent store.

    The caller chooses the database URL once when constructing ``store``.
    This unauthenticated local router is fixed to the ``demo`` tenant; callers
    cannot use a request parameter to select another tenant.
    """
    router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
    from knowledge_layer.build_transport import create_build_router

    router.include_router(create_build_router(store))
    queue = RefreshQueue(store) if durable_jobs else None

    from knowledge_layer.guidance_api import create_guidance_router

    router.include_router(create_guidance_router(store, allow_publication=True))
    from knowledge_layer.consumer_api import create_consumer_router

    router.include_router(create_consumer_router(store), prefix="/consumer")
    from knowledge_layer.inventory_transport import create_inventory_router

    router.include_router(create_inventory_router(store, allow_writes=True, allowed_kinds=allowed_kinds,
                                                  allow_curation_review=True))
    from knowledge_layer.autonomy_transport import create_autonomy_router

    router.include_router(create_autonomy_router(store, browser=True, enable_autonomy=enable_autonomy))

    @router.get("/workspace/graph")
    @router.get("/workspace/ontology")
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
            graph = workspace_graph(store, tenant_id="demo", connection_id=connection_id, q=q,
                                    focus_id=focus_id, depth=depth, limit=limit,
                                    source_limit=source_limit, cursor=cursor)
            return workspace_ontology(graph) if request.url.path.endswith("/ontology") else graph
        except (KnowledgeValidationError, KnowledgeNotFound, KnowledgeAccessDenied, KnowledgeConflict) as exc:
            _raise_http(exc)

    def require_enabled(kind: str) -> None:
        if allowed_kinds is not None and kind not in allowed_kinds:
            raise HTTPException(status_code=403, detail="This connector kind is not enabled by the operator")

    @router.get("/connector-types")
    def connector_types() -> dict[str, Any]:
        options = store.connector_types()
        return {"connector_types": [item for item in options if allowed_kinds is None or item["id"] in allowed_kinds]}

    @router.get("/connections")
    def list_connections(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=512),
    ) -> dict[str, Any]:
        try:
            page = store.page_connections("demo", limit=limit, cursor=cursor)
        except (KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)
        connections = page["connections"]
        jobs = queue.active_for_connections("demo", [item["id"] for item in connections]) if queue else {}
        return {"connections": [
            _connection_summary(connection) | ({"refresh_job": jobs.get(connection["id"])} if queue else {})
            for connection in connections
        ], "next_cursor": page["next_cursor"], "total": page["total"]}

    @router.post("/connections", status_code=status.HTTP_201_CREATED)
    def create_connection(body: ConnectionCreateRequest) -> dict[str, Any]:
        require_enabled(body.kind)
        try:
            connection = store.create_connection(
                "demo",
                kind=body.kind,
                name=body.name,
                config=body.config,
            )
        except (KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)
        return {"connection": connection}

    @router.get("/connections/{connection_id}")
    def get_connection(connection_id: str) -> dict[str, Any]:
        connection = store.get_connection("demo", connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        snapshot = connection.pop("snapshot", None)
        if queue is not None:
            connection["refresh_job"] = queue.active_for_connections("demo", [connection_id]).get(connection_id)
        return {"connection": connection, "snapshot": snapshot}

    @router.patch("/connections/{connection_id}")
    def update_connection(connection_id: str, body: ConnectionConfigRequest) -> dict[str, Any]:
        try:
            current = store._management_source("demo", connection_id)
            require_enabled(current["kind"])
            connection = store.update_connection_config("demo", connection_id, body.config)
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)
        snapshot = connection.pop("snapshot", None)
        return {"connection": connection, "snapshot": snapshot}

    @router.post("/connections/{connection_id}/refresh",
                 status_code=status.HTTP_202_ACCEPTED if durable_jobs else status.HTTP_200_OK)
    def refresh_connection(connection_id: str) -> dict[str, Any]:
        try:
            current = store._management_source("demo", connection_id)
            require_enabled(current["kind"])
            if queue is not None:
                return {"job": queue.enqueue("demo", connection_id)}
            result = store.refresh_connection("demo", connection_id)
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied, KnowledgeConflict, CurationFailed) as exc:
            _raise_http(exc)
        connection = result["connection"]
        connection.pop("snapshot", None)
        return {"connection": connection, "snapshot": result["snapshot"]} | (
            {"inventory": result["inventory"]} if result.get("inventory") is not None else {})

    @router.get("/connections/{connection_id}/refresh-schedule")
    def get_refresh_schedule(connection_id: str) -> dict[str, Any]:
        try:
            return {"schedule": RefreshSchedules(store).get("demo", connection_id)}
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)

    @router.put("/connections/{connection_id}/refresh-schedule")
    def set_refresh_schedule(connection_id: str, body: RefreshScheduleRequest) -> dict[str, Any]:
        try:
            current = store._management_source("demo", connection_id)
            require_enabled(current["kind"])
            return {"schedule": RefreshSchedules(store).set("demo", connection_id, **body.model_dump()),
                    "execution_requires": "knowledge-layer-worker with this connector kind enabled"}
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)

    if queue is not None:
        @router.get("/jobs/{job_id}")
        def get_refresh_job(job_id: str) -> dict[str, Any]:
            job = queue.get("demo", job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Refresh job not found")
            return {"job": job}

        @router.post("/jobs/{job_id}/cancel")
        def cancel_refresh_job(job_id: str) -> dict[str, Any]:
            try:
                return {"job": queue.cancel("demo", job_id)}
            except (KnowledgeNotFound, KnowledgeAccessDenied) as exc:
                _raise_http(exc)

    @router.get("/connections/{connection_id}/concepts")
    def connection_concepts(connection_id: str) -> dict[str, Any]:
        try:
            concepts = store.get_concepts("demo", connection_id)
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)
        return {"concepts": concepts}

    @router.get("/connections/{connection_id}/graph")
    def connection_graph(
        connection_id: str,
        focus_id: str | None = Query(default=None, min_length=1, max_length=160),
        depth: int = Query(default=1, ge=0, le=2),
        limit: int = Query(default=60, ge=1, le=100),
        version: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        try:
            return store.get_graph("demo", connection_id, focus_id=focus_id, depth=depth, limit=limit, version=version)
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied) as exc:
            _raise_http(exc)

    @router.get("/connections/{connection_id}/federated-graph")
    def connection_federated_graph(
        connection_id: str,
        limit: int = Query(default=6, ge=1, le=12),
    ) -> dict[str, Any]:
        try:
            return federated_evidence_graph(store, tenant_id="demo", connection_id=connection_id, limit=limit)
        except (KnowledgeNotFound, KnowledgeValidationError) as exc:
            _raise_http(exc)

    @router.get("/connections/{connection_id}/proposals")
    def connection_proposals(connection_id: str) -> dict[str, Any]:
        connection = store.get_connection("demo", connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        snapshot = connection.get("snapshot") or {}
        return {"proposals": snapshot.get("proposals", [])}

    @router.post("/connections/{connection_id}/proposals", status_code=status.HTTP_201_CREATED)
    def submit_proposal(
        connection_id: str,
        body: SemanticProposalRequest,
    ) -> dict[str, Any]:
        try:
            proposal = store.submit_proposal(
                "demo",
                connection_id,
                target_concept_id=body.target_concept_id,
                proposed_definition=body.proposed_definition,
                proposed_mapping=body.proposed_mapping,
                evidence_ids=body.evidence_ids,
                rationale=body.rationale,
                proposal_type=body.proposal_type,
                expected_snapshot_id=body.expected_snapshot_id,
            )
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied, KnowledgeConflict) as exc:
            _raise_http(exc)
        return {"proposal": proposal}

    @router.post("/connections/{connection_id}/proposals/{proposal_id}/decision")
    def decide_proposal(
        connection_id: str,
        proposal_id: str,
        body: ProposalDecisionRequest,
    ) -> dict[str, Any]:
        try:
            result = store.decide_proposal(
                "demo",
                connection_id,
                proposal_id,
                decision=body.decision,
                rationale=body.rationale,
            )
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied, KnowledgeConflict) as exc:
            _raise_http(exc)
        return result

    @router.post("/connections/{connection_id}/proposals/archive")
    def archive_proposals(connection_id: str, body: ProposalArchiveRequest) -> dict[str, Any]:
        try:
            return store.archive_proposals(
                "demo", connection_id, proposal_ids=body.proposal_ids, reason=body.reason,
            )
        except (KnowledgeNotFound, KnowledgeValidationError, KnowledgeAccessDenied, KnowledgeConflict) as exc:
            _raise_http(exc)

    @router.post("/connections/{connection_id}/review-batch/preview")
    def preview_batch(connection_id: str, body: ReviewBatchPreviewRequest) -> dict[str, Any]:
        from knowledge_layer.review_batches import preview_review_batch

        try:
            return preview_review_batch(store, "demo", connection_id, **body.model_dump())
        except KnowledgeError as exc:
            _raise_http(exc)

    @router.post("/connections/{connection_id}/review-batch/accept")
    def accept_batch(connection_id: str, body: ReviewBatchAcceptanceRequest) -> dict[str, Any]:
        from knowledge_layer.review_batches import accept_review_batch

        try:
            return accept_review_batch(store, "demo", connection_id, **body.model_dump())
        except KnowledgeError as exc:
            _raise_http(exc)

    @router.get("/connections/{connection_id}/publication")
    def publication_preview(connection_id: str) -> dict[str, Any]:
        try:
            return store.publication_preview("demo", connection_id)
        except (KnowledgeNotFound, KnowledgeAccessDenied) as exc:
            _raise_http(exc)

    @router.post("/connections/{connection_id}/publication")
    def publish_source(connection_id: str, body: PublicationRequest) -> dict[str, Any]:
        try:
            publication = store.publish_source(
                "demo", connection_id,
                expected_snapshot_id=body.expected_snapshot_id,
                expected_snapshot_version=body.expected_snapshot_version,
                rationale=body.rationale,
            )
        except (KnowledgeNotFound, KnowledgeAccessDenied, KnowledgeValidationError, KnowledgeConflict) as exc:
            _raise_http(exc)
        return {"publication": publication}

    @router.post("/connections/{connection_id}/publication/withdraw")
    def revoke_publication(connection_id: str, body: PublicationWithdrawalRequest) -> dict[str, Any]:
        try:
            return store.revoke_publication("demo", connection_id, reason=body.reason)
        except (KnowledgeNotFound, KnowledgeAccessDenied, KnowledgeValidationError, KnowledgeConflict) as exc:
            _raise_http(exc)

    @router.get("/concepts/{concept_id}")
    def get_concept(concept_id: str) -> dict[str, Any]:
        try:
            context = store.get_context("demo", concept_id=concept_id)
        except KnowledgeError as exc:
            _raise_http(exc)
        if context is None:
            raise HTTPException(status_code=404, detail="Concept not found")
        return context

    @router.get("/context")
    def get_context(
        term: str | None = Query(default=None, min_length=1, max_length=120),
        concept_id: str | None = Query(default=None, min_length=1, max_length=160),
    ) -> dict[str, Any]:
        if bool(term) == bool(concept_id):
            raise HTTPException(status_code=422, detail="Pass exactly one of term or concept_id")
        try:
            context = store.get_context("demo", concept_id=concept_id, term=term)
        except KnowledgeError as exc:
            _raise_http(exc)
        if context is None:
            raise HTTPException(status_code=404, detail="Concept not found")
        return context

    return router


def _connection_summary(connection: dict[str, Any]) -> dict[str, Any]:
    """Bound list responses while excluding per-connection config and snapshots."""
    fields = (
        "id", "tenant_id", "name", "kind", "connector_type", "status",
        "created_at", "updated_at", "last_refreshed_at", "last_synced_at",
        "last_refresh_attempt_at", "last_complete_refresh_at",
        "asset_count", "concept_count", "relationship_count", "proposal_count",
        "needs_attention_count", "stale_assertion_count", "coverage", "error", "stage",
        "agent_ready",
    )
    return {field: connection[field] for field in fields if field in connection}


def _raise_http(error: Exception) -> None:
    if isinstance(error, KnowledgeNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, KnowledgeAccessDenied):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, KnowledgeConflict):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, CurationFailed):
        code = status.HTTP_502_BAD_GATEWAY
    else:
        code = 422
    raise HTTPException(status_code=code, detail=str(error)) from error
