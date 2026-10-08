"""Consumer HTTP operations shared by browser and external harness surfaces."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from knowledge_layer.mcp import ContractResponseTooLarge, _complete_contract_response
from knowledge_layer.store import KnowledgeConflict, KnowledgeError, KnowledgeNotFound


class AgentMetricPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assertion_id: str = Field(min_length=1, max_length=160)
    request: dict[str, Any] = Field(default_factory=dict)


class InventoryTaskSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(min_length=1, max_length=64)
    concept_ids: list[str] = Field(min_length=1, max_length=20)


class AgentTaskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=120)
    connection_ids: list[str] = Field(min_length=1, max_length=8)
    business_unit: str | None = Field(default=None, max_length=120)
    task: str | None = Field(default=None, max_length=120)
    inventory_selection: list[InventoryTaskSelection] | None = Field(default=None, min_length=1, max_length=8)


def create_consumer_router(store: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/maintenance-work")
    def maintenance(connection_id: str | None = Query(default=None, min_length=1, max_length=64),
                    cursor: str | None = Query(default=None, max_length=512),
                    limit: int = Query(default=20, ge=1, le=20)) -> dict[str, Any]:
        from knowledge_layer.curation_handoff import maintenance_work
        try:
            return _complete_contract_response(maintenance_work(
                store, tenant_id="demo", connection_id=connection_id, cursor=cursor, limit=limit,
            ))
        except KnowledgeNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @router.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        from knowledge_layer.capabilities import capability_manifest
        return capability_manifest()

    @router.post("/task-context")
    def resolve_task_context(body: AgentTaskContext) -> dict[str, Any]:
        from knowledge_layer.task_context import task_context
        try:
            return task_context(store, tenant_id="demo", **body.model_dump())
        except KnowledgeNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/task-context/revalidate")
    def validate_task_context(receipt: dict[str, Any]) -> dict[str, Any]:
        from knowledge_layer.task_context import revalidate_context
        try:
            return revalidate_context(store, tenant_id="demo", receipt=receipt)
        except KnowledgeNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/sources/{connection_id}/metric-plan")
    def metric_query_plan(connection_id: str, body: AgentMetricPlan) -> dict[str, Any]:
        from knowledge_layer.planning import metric_plan
        try:
            return _complete_contract_response(metric_plan(
                store, tenant_id="demo", connection_id=connection_id,
                assertion_id=body.assertion_id, request=body.request,
            ))
        except KnowledgeNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    @router.get("/sources/{connection_id}/relationships/{assertion_id}/plan")
    def cross_source_plan(connection_id: str, assertion_id: str) -> dict[str, Any]:
        from knowledge_layer.cross_source import relationship_plan
        try:
            return _complete_contract_response(relationship_plan(
                store, tenant_id="demo", connection_id=connection_id, assertion_id=assertion_id,
            ))
        except KnowledgeNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc

    return router
