"""Bounded delegation transport; agents cannot draft or approve authority.

Authentication belongs to the enclosing host. The registry independently checks
the verified principal, direct source grant, operator policy and current binding.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from knowledge_layer.autonomy import AutonomyRegistry
from knowledge_layer.mcp import (
    ContractResponseTooLarge,
    _complete_contract_response,
    _contract_tool_response,
)
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeValidationError,
)

_TEXT_PATTERN = r"^[^\x00-\x1f\x7f]+$"
ConnectionId = Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=_TEXT_PATTERN)]
Revision = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
Rationale = Annotated[StrictStr, Field(min_length=16, max_length=1000, pattern=_TEXT_PATTERN)]
_CONNECTION_ID = TypeAdapter(ConnectionId)


class AutonomyPolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["knowledge-autonomy-policy-v1"]
    action: Literal["publish_reviewed_unchanged"]
    subject: Annotated[StrictStr, Field(min_length=1, max_length=255, pattern=_TEXT_PATTERN)]
    expires_at: Annotated[StrictStr, Field(min_length=1, max_length=40)]
    max_publications: Annotated[StrictInt, Field(ge=1, le=1000)]
    purpose: Rationale


class AutonomyGetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Revision | None = None


class AutonomyDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: AutonomyPolicyPayload
    expected_revision: Annotated[StrictInt, Field(ge=0, le=2**31 - 2)] = 0


class AutonomyApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Revision
    expected_digest: Digest
    rationale: Rationale


class AutonomyWithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Revision
    expected_digest: Digest
    reason: Rationale


class AutonomyApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: Revision
    expected_digest: Digest
    expected_snapshot_id: Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=_TEXT_PATTERN)]
    expected_snapshot_version: Revision


_REQUESTS = {
    "get": AutonomyGetRequest,
    "draft": AutonomyDraftRequest,
    "approve": AutonomyApprovalRequest,
    "revoke": AutonomyWithdrawalRequest,
    "apply": AutonomyApplyRequest,
}


def autonomy_operation(store: Any, connection_id: str, operation: str,
                       payload: dict[str, Any], *, enable_execution: bool = False,
                       tenant_id: str = "demo") -> dict[str, Any]:
    """Bind only validated arguments to the fixed host tenant and registry."""
    if operation not in _REQUESTS:
        raise KnowledgeValidationError("Autonomy operation is unsupported")
    if operation == "apply" and not enable_execution:
        raise KnowledgeAccessDenied("Autonomy execution is not enabled by the operator")
    try:
        cid = _CONNECTION_ID.validate_python(connection_id)
        arguments = _REQUESTS[operation].model_validate(payload).model_dump()
    except ValidationError as exc:
        raise KnowledgeValidationError("Autonomy operation has invalid or unsupported fields") from exc
    registry = AutonomyRegistry(store, execution_enabled=enable_execution)
    if operation == "draft":
        return registry.draft(tenant_id, cid, arguments["payload"],
                              expected_revision=arguments["expected_revision"])
    return getattr(registry, operation)(tenant_id, cid, **arguments)


def create_autonomy_router(store: Any, *, browser: bool = False,
                           enable_autonomy: bool = False) -> APIRouter:
    """Browser policy management or explicit opt-in agent get/apply routes."""
    router = APIRouter(tags=["bounded autonomy"])
    if not browser and not enable_autonomy:
        return router
    prefix = "/connections" if browser else "/sources"
    path = prefix + "/{connection_id}/autonomy"

    def respond(cid: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return _complete_contract_response(autonomy_operation(
                store, cid, operation, arguments, enable_execution=enable_autonomy,
            ))
        except KnowledgeNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except KnowledgeAccessDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except KnowledgeConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ContractResponseTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc

    @router.get(path)
    def get_policy(
        connection_id: Annotated[str, Path(min_length=1, max_length=64, pattern=_TEXT_PATTERN)],
        revision: Annotated[str | None, Query(pattern=r"^[1-9][0-9]{0,9}$")] = None,
    ) -> dict[str, Any]:
        # Query strings have no JSON integer type. Refuse fractional, signed,
        # boolean or oversized forms before converting a bounded decimal token.
        return respond(connection_id, "get", {"revision": int(revision) if revision is not None else None})

    if browser:
        @router.post(path + "/draft", status_code=201)
        def draft(connection_id: ConnectionId, body: AutonomyDraftRequest) -> dict[str, Any]:
            return respond(connection_id, "draft", body.model_dump())

        @router.post(path + "/approve")
        def approve(connection_id: ConnectionId, body: AutonomyApprovalRequest) -> dict[str, Any]:
            return respond(connection_id, "approve", body.model_dump())

        @router.post(path + "/revoke")
        def revoke(connection_id: ConnectionId, body: AutonomyWithdrawalRequest) -> dict[str, Any]:
            return respond(connection_id, "revoke", body.model_dump())
    else:
        @router.post(path + "/apply")
        def apply(connection_id: ConnectionId, body: AutonomyApplyRequest) -> dict[str, Any]:
            return respond(connection_id, "apply", body.model_dump())
    return router


def register_autonomy_tools(server: Any, store: Any, *, enable_autonomy: bool = False,
                            tenant_id: str = "demo") -> None:
    if not enable_autonomy:
        return

    def respond(cid: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            return _contract_tool_response(autonomy_operation(
                store, cid, operation, payload, enable_execution=True, tenant_id=tenant_id,
            ))
        except KnowledgeError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="Read a source manager's bounded autonomy policy and exact revision. This policy is separately steward-approved authority, not advisory company guidance. No source query or write is performed; active policy alone does not establish permission for this caller.")
    def get_autonomy_policy(connection_id: ConnectionId, revision: Revision | None = None) -> dict[str, Any]:
        return respond(connection_id, "get", {"revision": revision})

    @server.tool(description="Apply only a separately steward-approved publish_reviewed_unchanged policy to its exact current reviewed snapshot. Requires operator opt-in, verified knowledge.autonomy scope, exact subject, direct source manage grant and current operator source approval. No new proposal approval, source query, source write or generic execution is available. Stale/changed/expired/revoked bindings refuse.")
    def apply_autonomy_policy(connection_id: ConnectionId, request: AutonomyApplyRequest) -> dict[str, Any]:
        return respond(connection_id, "apply", request.model_dump())
