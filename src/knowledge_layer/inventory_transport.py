"""Bounded inventory reads, discovery staging and separate local concept curation."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from knowledge_layer.inventory import InventoryStore
from knowledge_layer.mcp import (
    READ_ONLY_ANNOTATIONS,
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

WriteOperation = Literal["begin", "append", "commit", "abort"]
CurateOperation = Literal["propose_concept"]
_READS = {"status", "current", "page", "scopes", "select", "concepts", "concept"}
_KEYS = {
    "begin": {"scope", "policy", "watermark", "limits", "expected_counts"},
    "append": {"inventory_id", "page_index", "cursor", "next_cursor", "entities", "evidence",
               "scope_digest", "policy_digest", "watermark_digest"},
    "commit": {"inventory_id", "completion"}, "abort": {"inventory_id"},
    "status": {"inventory_id"}, "current": {"scope_digest", "policy_digest"},
    "page": {"inventory_id", "collection", "after", "limit"},
    "scopes": {"after", "limit"},
    "select": {"inventory_id", "expected_manifest_digest", "entity_ids"},
    "concepts": {"inventory_id", "expected_manifest_digest", "after", "limit", "include_drafts", "query"},
    "concept": {"concept_id"},
    "propose_concept": {"evidence_ref", "name", "definition", "concept_kind", "rationale"},
    "review_concept": {"concept_id", "decision", "rationale"},
    "publish_concept": {"concept_id", "expected_proposal_digest", "rationale"},
    "withdraw_concept": {"concept_id", "reason"},
}
_CONCEPT_METHODS = {
    "concepts": "page", "concept": "get", "propose_concept": "propose",
    "review_concept": "decide", "publish_concept": "publish", "withdraw_concept": "withdraw",
}


def inventory_operation(store: Any, connection_id: str, operation: str, payload: dict[str, Any], *,
                        allow_writes: bool = False, allowed_kinds: frozenset[str] | None = None,
                        tenant_id: str = "demo") -> dict[str, Any]:
    if operation not in _KEYS or (operation not in _READS and not allow_writes):
        raise KnowledgeAccessDenied("Inventory operation is not enabled")
    if not isinstance(payload, dict) or not set(payload).issubset(_KEYS[operation]):
        raise KnowledgeValidationError("Inventory operation contains unsupported fields")
    try:
        width = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, UnicodeError) as exc:
        raise KnowledgeValidationError("Inventory operation requires finite UTF-8 JSON") from exc
    if width > 256 * 1024:
        raise KnowledgeValidationError("Inventory operation exceeds 256 KiB; use smaller complete pages")
    if operation not in _READS and allowed_kinds is not None:
        # Do not project a legacy aggregate snapshot just to read staged rows.
        # InventoryStore independently rechecks source approval and manage rights
        # inside its mutation transaction.
        with store.engine.connect() as conn:
            kind = conn.execute(select(store.connections.c.kind).where(
                store.connections.c.tenant_id == tenant_id, store.connections.c.id == connection_id,
                store._access_clause(tenant_id, store.connections.c.id),
            )).scalar_one_or_none()
        if kind is None:
            raise KnowledgeNotFound("Inventory source was not found")
        if kind not in allowed_kinds:
            raise KnowledgeAccessDenied("This source kind is not enabled for inventory ingestion")
    if operation in _CONCEPT_METHODS:
        from knowledge_layer.inventory_concepts import InventoryConcepts

        method = getattr(InventoryConcepts(store), _CONCEPT_METHODS[operation])
    else:
        method = getattr(InventoryStore(store), operation)
    # Bind before execution: caller data cannot supply claim/principal/tenant or
    # turn malformed arguments into unexpected application exceptions.
    from inspect import signature
    try:
        signature(method).bind(tenant_id, connection_id, **payload)
    except TypeError as exc:
        raise KnowledgeValidationError("Inventory operation is missing required arguments") from exc
    result = method(tenant_id, connection_id, **payload)
    return result if result is not None else {"status": "not_established", "inventory": None, "agent_ready": False}


class InventoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]


def create_inventory_router(store: Any, *, allow_writes: bool = False,
                            allowed_kinds: frozenset[str] | None = None,
                            allow_curation_review: bool = False) -> APIRouter:
    router = APIRouter()

    def respond(cid, operation, payload):
        try:
            return _complete_contract_response(inventory_operation(
                store, cid, operation, payload, allow_writes=allow_writes, allowed_kinds=allowed_kinds))
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

    @router.get("/sources/{connection_id}/inventory/current")
    def current(connection_id: str, scope_digest: str = Query(pattern="^[a-f0-9]{64}$"),
                policy_digest: str = Query(pattern="^[a-f0-9]{64}$")):
        return respond(connection_id, "current", {"scope_digest": scope_digest, "policy_digest": policy_digest})

    @router.get("/sources/{connection_id}/inventory/scopes")
    def scopes(connection_id: str, after: str | None = Query(default=None, max_length=1024),
               limit: int = Query(default=10, ge=1, le=20)):
        return respond(connection_id, "scopes", {"after": after, "limit": limit})

    @router.post("/sources/{connection_id}/inventory/{inventory_id}/select")
    def select_entities(connection_id: str, inventory_id: str, body: InventoryCommand):
        if "inventory_id" in body.payload:
            raise HTTPException(422, "Inventory selection ID belongs in the route")
        return respond(connection_id, "select", {**body.payload, "inventory_id": inventory_id})

    @router.get("/sources/{connection_id}/inventory/{inventory_id}/concepts")
    def concepts(
        connection_id: str, inventory_id: str,
        expected_manifest_digest: str = Query(pattern="^[a-f0-9]{64}$"),
        after: str | None = Query(default=None, max_length=1024),
        limit: int = Query(default=10, ge=1, le=20), include_drafts: bool = False,
        query: str | None = Query(default=None, max_length=200),
    ):
        return respond(connection_id, "concepts", {
            "inventory_id": inventory_id, "expected_manifest_digest": expected_manifest_digest,
            "after": after, "limit": limit, "include_drafts": include_drafts, "query": query,
        })

    @router.get("/sources/{connection_id}/inventory-concepts/{concept_id}")
    def concept(connection_id: str, concept_id: str):
        return respond(connection_id, "concept", {"concept_id": concept_id})

    @router.get("/sources/{connection_id}/inventory/{inventory_id}")
    def status(connection_id: str, inventory_id: str):
        return respond(connection_id, "status", {"inventory_id": inventory_id})

    @router.get("/sources/{connection_id}/inventory/{inventory_id}/page")
    def page(connection_id: str, inventory_id: str, collection: Literal["entities", "evidence", "removed"] = "entities",
             after: str | None = Query(default=None, max_length=1024), limit: int = Query(default=100, ge=1, le=100)):
        return respond(connection_id, "page", {"inventory_id": inventory_id, "collection": collection,
                                               "after": after, "limit": limit})

    if allow_writes:
        @router.post("/sources/{connection_id}/inventory/{operation}")
        def write(connection_id: str, operation: WriteOperation, body: InventoryCommand):
            return respond(connection_id, operation, body.payload)

        @router.post("/sources/{connection_id}/inventory-concepts/propose_concept")
        def propose_inventory_concept(connection_id: str, body: InventoryCommand):
            return respond(connection_id, "propose_concept", body.payload)

        if allow_curation_review:
            @router.post("/sources/{connection_id}/inventory-concepts/review_concept")
            def review_inventory_concept(connection_id: str, body: InventoryCommand):
                return respond(connection_id, "review_concept", body.payload)

            @router.post("/sources/{connection_id}/inventory-concepts/publish_concept")
            def publish_inventory_concept(connection_id: str, body: InventoryCommand):
                return respond(connection_id, "publish_concept", body.payload)

            @router.post("/sources/{connection_id}/inventory-concepts/withdraw_concept")
            def withdraw_inventory_concept(connection_id: str, body: InventoryCommand):
                return respond(connection_id, "withdraw_concept", body.payload)

    return router


def register_inventory_tools(server: Any, store: Any, *, allow_writes: bool = False,
                             allowed_kinds: frozenset[str] | None = None,
                             tenant_id: str = "demo") -> None:
    def respond(connection_id, operation, payload):
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            return _contract_tool_response(inventory_operation(store, connection_id, operation, payload,
                allow_writes=allow_writes, allowed_kinds=allowed_kinds, tenant_id=tenant_id))
        except KnowledgeError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ_ONLY_ANNOTATIONS, description="Read current inventory scopes, manifests, bounded entity/evidence/removal pages or exact same-manifest selections. Use concepts for a manifest-pinned page of current locally published concepts (drafts require source steward access), or concept for full evidence-backed context. Discovered native text remains untrusted; local review/publication is separate and grants no source execution permission.")
    def get_inventory(connection_id: str, operation: Literal["status", "current", "page", "scopes", "select", "concepts", "concept"],
                      payload: dict[str, Any]) -> dict[str, Any]:
        return respond(connection_id, operation, payload)

    if allow_writes:
        @server.tool(description="Stage operator-enabled source metadata in bounded pages: begin, append, commit or abort. Requires source manager and manage grant; checkpoints bind owner, scope, policy and watermark. Complete discovery changes no source data and grants no approval/publication. Incomplete/replayed-changed/stale-worker pages refuse.")
        def stage_inventory(connection_id: str, operation: WriteOperation,
                            payload: dict[str, Any]) -> dict[str, Any]:
            return respond(connection_id, operation, payload)

        @server.tool(description="Propose a local concept from exact current inventory evidence for owner review. Requires operator-enabled curation and source read access; caller data cannot supply authority. This tool cannot review, publish or withdraw concepts. Owner review/publication remain separate and grant no source execution permission.")
        def curate_inventory(connection_id: str, operation: CurateOperation,
                             payload: dict[str, Any]) -> dict[str, Any]:
            return respond(connection_id, operation, payload)
