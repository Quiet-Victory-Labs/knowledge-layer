"""Isolated agent-facing ASGI app; excludes the legacy demo APIs."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.mcp_transport import McpEnvelopeBoundary
from knowledge_layer.request_budget import HttpRequestBudget
from knowledge_layer.store import KnowledgeStore


def create_gateway(
    *, store: KnowledgeStore | None = None, agent_token: str | None = None,
    enable_source_management: bool | None = None,
    allowed_connection_kinds: frozenset[str] | None = None,
) -> FastAPI:
    knowledge_store = store or KnowledgeStore()
    enabled = enable_source_management if enable_source_management is not None else os.getenv("KNOWLEDGE_AGENT_SOURCE_MANAGEMENT") == "1"
    allowed = allowed_connection_kinds if allowed_connection_kinds is not None else frozenset(
        value.strip() for value in os.getenv("KNOWLEDGE_AGENT_ALLOWED_KINDS", "local_files").split(",") if value.strip()
    )
    mcp_server = build_knowledge_mcp_server(knowledge_store, enable_source_management=enabled, allowed_kinds=allowed)
    token = agent_token if agent_token is not None else os.getenv("KNOWLEDGE_AGENT_TOKEN")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_server.session_manager.run():
            yield

    private = FastAPI(title="Knowledge Layer Agent Gateway", lifespan=lifespan)
    private.add_middleware(HttpRequestBudget)
    private.mount("/api", build_agent_api(knowledge_store, enable_source_management=enabled, allowed_kinds=allowed))
    private.mount(
        "/mcp",
        McpEnvelopeBoundary(mcp_server.streamable_http_app(
            streamable_http_path="/", json_response=True, stateless_http=True
        )),
    )
    public = FastAPI(title="Knowledge Layer Gateway Boundary", lifespan=lifespan)
    public.mount("/agent", AgentBearerBoundary(private, token))
    public.state.knowledge_store = knowledge_store
    public.state.mcp_server = mcp_server
    return public


app = create_gateway()
