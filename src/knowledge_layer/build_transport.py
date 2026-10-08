"""Equivalent browser/HTTP/MCP transports for scoped durable build work."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from knowledge_layer.builds import BuildRuns
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeValidationError,
)


class BuildStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_ids: list[str] = Field(min_length=1, max_length=8)
    max_items: StrictInt = Field(default=200, ge=1, le=500)
    max_seconds: StrictInt = Field(default=3600, ge=60, le=86400)


class BuildCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=8, max_length=1000)


class BuildClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_seconds: StrictInt = Field(default=300, ge=30, le=900)


class BuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    lease_token: str = Field(min_length=32, max_length=64)
    result: dict[str, Any]


def build_operation(store, *, operation: str, arguments: dict, tenant_id: str = "demo") -> dict:
    """Single compact MCP tool with strict operation-specific inputs."""
    runs = BuildRuns(store)
    if not isinstance(arguments, dict):
        raise KnowledgeValidationError("arguments must be an object")
    allowed = {"start": {"connection_ids", "max_items", "max_seconds"},
               "list": {"connection_id", "limit"}, "get": {"run_id"},
               "claim": {"run_id", "lease_seconds"},
               "complete": {"run_id", "task_id", "lease_token", "result"},
               "cancel": {"run_id", "reason"}}
    if operation not in allowed or set(arguments) - allowed[operation]:
        raise KnowledgeValidationError("Unknown build operation or argument")
    values = dict(arguments)
    if operation == "start":
        if "connection_ids" not in values:
            raise KnowledgeValidationError("connection_ids are required")
        return {"build": runs.start(tenant_id, **values)}
    if operation == "list":
        return {"builds": runs.list(tenant_id, **values)}
    run_id = values.pop("run_id", None)
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 64:
        raise KnowledgeValidationError("run_id is required")
    if operation == "get":
        return {"build": runs.get(tenant_id, run_id)}
    if operation == "claim":
        return runs.claim(tenant_id, run_id, **values)
    required = {"complete": {"task_id", "lease_token", "result"}, "cancel": {"reason"}}[operation]
    if not required <= set(values):
        raise KnowledgeValidationError("Required build result/cancellation arguments are missing")
    if operation == "complete":
        if any(not isinstance(values[key], str) or not 1 <= len(values[key]) <= 64
               for key in ("task_id", "lease_token")):
            raise KnowledgeValidationError("Bounded task_id and lease_token are required")
        return runs.complete(tenant_id, run_id, **values)
    return {"build": runs.cancel(tenant_id, run_id, **values)}


def create_build_router(store, *, agent=False) -> APIRouter:
    router = APIRouter(prefix="/builds", tags=["builds"])

    def call(operation, *args, **kwargs):
        try:
            return getattr(BuildRuns(store), operation)("demo", *args, **kwargs)
        except KnowledgeError as exc:
            code = (404 if isinstance(exc, KnowledgeNotFound) else
                    403 if isinstance(exc, KnowledgeAccessDenied) else
                    409 if isinstance(exc, KnowledgeConflict) else 422)
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @router.post("")
    def start(payload: BuildStart):
        return {"build": call("start", **payload.model_dump())}

    @router.get("")
    def list_builds(connection_id: str | None = Query(default=None, min_length=1, max_length=64),
                    limit: int = Query(default=20, ge=1, le=50)):
        return {"builds": call("list", connection_id=connection_id, limit=limit)}

    @router.get("/{run_id}")
    def get(run_id: str):
        return {"build": call("get", run_id)}

    @router.post("/{run_id}/cancel")
    def cancel(run_id: str, payload: BuildCancel):
        return {"build": call("cancel", run_id, **payload.model_dump())}

    if agent:
        @router.post("/{run_id}/claim")
        def claim(run_id: str, payload: BuildClaim):
            return call("claim", run_id, **payload.model_dump())

        @router.post("/{run_id}/complete")
        def complete(run_id: str, payload: BuildResult):
            return call("complete", run_id, **payload.model_dump())
    return router


def register_build_tools(server, store, *, tenant_id: str = "demo"):
    @server.tool(description=(
        "Start, inspect, lease, checkpoint or cancel a durable knowledge build. "
        "operation=start takes connection_ids,max_items,max_seconds; list takes connection_id,limit; "
        "get/claim take run_id (claim optional lease_seconds). complete takes run_id,task_id,lease_token,result. "
        "result is {action:propose|abstain|defer|error,reason,evidence_ids,proposed_definition/proposed_mapping "
        "for meaning or payload for contracts}. Claim returns exact pinned stored evidence; "
        "complete commits a pending human-review draft and receipt atomically. "
        "cancel takes run_id,reason. Never approve or publish; customer agents execute the work."))
    def build_knowledge(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        from knowledge_layer.mcp import _contract_tool_response
        try:
            return _contract_tool_response(build_operation(
                store, operation=operation, arguments=arguments, tenant_id=tenant_id,
            ))
        except KnowledgeError as exc:
            raise ToolError(str(exc)) from exc
