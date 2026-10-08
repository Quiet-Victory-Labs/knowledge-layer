"""Company guidance HTTP boundary; agents may draft but cannot publish."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeValidationError,
)


class GuidanceDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: dict[str, Any]
    expected_revision: StrictInt = Field(default=0, ge=0)


class GuidancePublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=16, max_length=1000)


class GuidanceWithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=1)
    reason: str = Field(min_length=16, max_length=1000)


def _raise_guidance_http(exc: Exception) -> None:
    code = (404 if isinstance(exc, KnowledgeNotFound) else
            403 if isinstance(exc, KnowledgeAccessDenied) else
            409 if isinstance(exc, KnowledgeConflict) else 422)
    raise HTTPException(status_code=code, detail=str(exc)) from exc


def create_guidance_router(store: Any, *, allow_publication: bool = False) -> APIRouter:
    from knowledge_layer.guidance import GuidanceRegistry
    from knowledge_layer.guidance_export import export_guidance

    router = APIRouter(prefix="/guidance", tags=["company guidance"])
    def registry() -> GuidanceRegistry:
        return GuidanceRegistry(store)
    errors = (KnowledgeNotFound, KnowledgeAccessDenied, KnowledgeConflict, KnowledgeValidationError)

    @router.get("/resolve")
    def resolve(
        business_unit: str | None = Query(default=None, min_length=1, max_length=120),
        task: str | None = Query(default=None, min_length=1, max_length=120),
        connection_ids: Annotated[list[str] | None, Query(max_length=20)] = None,
    ) -> dict[str, Any]:
        try:
            return registry().resolve("demo", business_unit=business_unit, task=task,
                                    connection_ids=connection_ids)
        except errors as exc:
            _raise_guidance_http(exc)

    @router.get("/export")
    def export(
        client: str = Query(default="portable", min_length=1, max_length=20),
        business_unit: str | None = Query(default=None, min_length=1, max_length=120),
        task: str | None = Query(default=None, min_length=1, max_length=120),
        connection_ids: Annotated[list[str] | None, Query(max_length=20)] = None,
    ) -> dict[str, Any]:
        try:
            resolution = registry().resolve("demo", business_unit=business_unit, task=task,
                                          connection_ids=connection_ids)
            return export_guidance(resolution, client=client)
        except (*errors, ValueError) as exc:
            _raise_guidance_http(exc)

    @router.get("/{slug}")
    def get(slug: str, revision: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
        try:
            result = registry().get("demo", slug, revision=revision)
            if result is None:
                raise KnowledgeNotFound("Guidance was not found")
            return result
        except errors as exc:
            _raise_guidance_http(exc)

    @router.post("/{slug}/draft", status_code=201)
    def draft(slug: str, body: GuidanceDraftRequest) -> dict[str, Any]:
        try:
            return registry().draft("demo", slug, body.payload,
                                  expected_revision=body.expected_revision)
        except errors as exc:
            _raise_guidance_http(exc)

    if allow_publication:
        @router.post("/{slug}/publish")
        def publish(slug: str, body: GuidancePublicationRequest) -> dict[str, Any]:
            try:
                return registry().publish("demo", slug, **body.model_dump())
            except errors as exc:
                _raise_guidance_http(exc)

        @router.post("/{slug}/withdraw")
        def withdraw(slug: str, body: GuidanceWithdrawalRequest) -> dict[str, Any]:
            try:
                return registry().revoke("demo", slug, **body.model_dump())
            except errors as exc:
                _raise_guidance_http(exc)

    return router
