"""Small evaluation host for the source-neutral knowledge layer."""

from __future__ import annotations

import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from knowledge_layer.access import principal_from_claims, principal_scope
from knowledge_layer.access_log import AccessLogBoundary
from knowledge_layer.agent_api import build_agent_api
from knowledge_layer.api import create_knowledge_router
from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.iap import IapBoundary, IapSettings, iap_router, require_iap_browser
from knowledge_layer.local_auth import (
    LocalAgentBoundary,
    LocalHostBoundary,
    LocalSettings,
    local_auth_router,
    require_local_browser,
)
from knowledge_layer.mcp import build_knowledge_mcp_server
from knowledge_layer.mcp_transport import McpEnvelopeBoundary
from knowledge_layer.oidc import (
    BrowserStaticBoundary,
    OidcAgentBoundary,
    OidcSettings,
    oidc_router,
    require_browser,
)
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.quick_mcp import build_quick_mcp_server
from knowledge_layer.request_budget import HttpRequestBudget
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import KnowledgeStore

_monotonic = time.monotonic


def create_platform(
    *, store: KnowledgeStore | None = None, agent_token: str | None = None
) -> FastAPI:
    """Serve the local demo or one managed OIDC/IAP organization."""
    auth_mode = os.getenv("KNOWLEDGE_AUTH_MODE", "demo")
    if auth_mode not in {"demo", "local", "oidc", "iap"}:
        raise ValueError("KNOWLEDGE_AUTH_MODE must be demo, local, oidc or iap")
    managed = auth_mode in {"local", "oidc", "iap"}
    autonomy = os.getenv("KNOWLEDGE_AGENT_AUTONOMY") == "1"
    if autonomy and not managed:
        raise ValueError("KNOWLEDGE_AGENT_AUTONOMY requires managed authentication")
    oidc_settings = OidcSettings() if auth_mode == "oidc" else None
    iap_settings = IapSettings() if auth_mode == "iap" else None
    local_settings = LocalSettings() if auth_mode == "local" else None
    managed_settings = oidc_settings or iap_settings or local_settings
    source_policy = SourcePolicy.from_environment() if auth_mode in {"oidc", "iap"} else None
    database_url = operator_secret("DATABASE_URL") if store is None else None
    if managed and store is None and not (database_url or "").startswith(
        ("postgres://", "postgresql://", "postgresql+psycopg://")
    ):
        raise ValueError("Managed mode requires a customer-managed PostgreSQL DATABASE_URL or DATABASE_URL_FILE")
    knowledge_store = store or KnowledgeStore(
        database_url=database_url, initialize_schema=not managed
    )
    schema_checked_at = _monotonic()
    schema_current = True
    schema_check_lock = Lock()
    if managed:
        from knowledge_layer.migrate import ensure_current_schema

        try:
            ensure_current_schema(knowledge_store)
        except Exception:
            if store is None:
                knowledge_store.close()
            raise
        knowledge_store.access_enforced = True
        knowledge_store.source_admin_group = managed_settings.steward_group
        if local_settings is not None:
            from knowledge_layer.local_profile import owner_source_policy

            source_policy = owner_source_policy(knowledge_store)
            knowledge_store.source_policy_loader = lambda: owner_source_policy(knowledge_store)
            knowledge_store.local_agent_subject = "local-agent"
        knowledge_store.source_policy = source_policy
        if local_settings is None:
            knowledge_store.source_policy_loader = SourcePolicy.from_environment
    enabled_kinds = frozenset(
        value.strip() for value in os.getenv("KNOWLEDGE_ENABLED_KINDS", "").split(",") if value.strip()
    ) if managed else None
    source_management = os.getenv("KNOWLEDGE_AGENT_SOURCE_MANAGEMENT") == "1"
    allowed_kinds = frozenset(
        value.strip()
        for value in os.getenv("KNOWLEDGE_AGENT_ALLOWED_KINDS", "" if managed else "local_files").split(",")
        if value.strip()
    )
    if enabled_kinds is not None:
        allowed_kinds &= enabled_kinds
    mcp_server = build_knowledge_mcp_server(
        knowledge_store, enable_source_management=source_management, allowed_kinds=allowed_kinds,
        durable_jobs=managed, enable_autonomy=autonomy,
    )
    quick_server = build_quick_mcp_server(
        knowledge_store, enable_source_management=source_management, allowed_kinds=allowed_kinds,
        durable_jobs=managed, enable_autonomy=autonomy,
    )
    token = None if managed else (agent_token if agent_token is not None else operator_secret("KNOWLEDGE_AGENT_TOKEN"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_server.session_manager.run())
            await stack.enter_async_context(quick_server.session_manager.run())
            yield

    app = FastAPI(
        title="Knowledge Layer Evaluation API",
        description="Small source-neutral connection and curation host.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.knowledge_store = knowledge_store
    app.state.auth_mode = auth_mode
    app.state.local_settings = local_settings
    if local_settings is not None:
        @app.exception_handler(RequestValidationError)
        async def private_auth_validation(request: Request, exc: RequestValidationError):
            if request.url.path.startswith("/auth/"):
                return JSONResponse({"detail": "Check the owner name, password length and bootstrap token"}, status_code=422)
            return await request_validation_exception_handler(request, exc)
    if managed:
        async def authorize_browser(request: Request):
            identity = (require_local_browser(request, local_settings) if local_settings is not None else
                        require_iap_browser(request, iap_settings) if iap_settings is not None
                        else require_browser(request, oidc_settings))
            with principal_scope(principal_from_claims(identity)):
                yield

        app.include_router(
            create_knowledge_router(store=knowledge_store, allowed_kinds=enabled_kinds, durable_jobs=True,
                                    enable_autonomy=autonomy),
            dependencies=[Depends(authorize_browser)],
        )
        app.include_router(local_auth_router(local_settings) if local_settings is not None else
                           iap_router(iap_settings) if iap_settings is not None else oidc_router(oidc_settings))
        if local_settings is None:
            app.add_middleware(
                SessionMiddleware, secret_key=managed_settings.session_key,
                session_cookie="knowledge_session", max_age=managed_settings.browser_session_seconds,
                same_site="lax", https_only=True,
            )
        app.add_middleware(AccessLogBoundary)
        if iap_settings is not None:
            app.add_middleware(IapBoundary, settings=iap_settings)
        if local_settings is not None:
            app.add_middleware(LocalHostBoundary, settings=local_settings)

        if oidc_settings is not None:
            @app.get("/.well-known/oauth-protected-resource")
            def protected_resource_metadata() -> dict[str, Any]:
                return {"resource": f"{oidc_settings.public_url}/agent",
                        "authorization_servers": [oidc_settings.issuer]}
    else:
        app.include_router(create_knowledge_router(store=knowledge_store))

        @app.get("/auth/me")
        def demo_browser_session():
            # Demo browser access is anonymous. Describe its existing operator
            # authority explicitly without issuing a managed session or token.
            return JSONResponse({"mode": "demo", "subject": "operator", "groups": [],
                                 "steward": True, "csrf_token": None},
                                headers={"Cache-Control": "no-store"})

    from knowledge_layer.setup import create_setup_router

    app.include_router(create_setup_router(knowledge_store, auth_mode=auth_mode,
                                          settings=managed_settings),
                       dependencies=[Depends(authorize_browser)] if managed else [])

    @app.get("/auth/status")
    def auth_status(request: Request):
        configured, authenticated = True, auth_mode == "demo"
        if local_settings is not None:
            try:
                configured = local_settings.owner() is not None
                local_settings.identity(request)
                authenticated = True
            except HTTPException:
                authenticated = False
            except (ValueError, OSError):
                raise HTTPException(503, "Private owner configuration is unavailable") from None
        elif oidc_settings is not None:
            from knowledge_layer.oidc import browser_identity
            try:
                browser_identity(request, oidc_settings)
                authenticated = True
            except HTTPException:
                authenticated = False
        elif iap_settings is not None:
            authenticated = bool(request.scope.get("knowledge_iap_identity"))
        return JSONResponse({"mode": auth_mode, "configured": configured, "authenticated": authenticated,
                             "bootstrap_required": local_settings is not None and not configured,
                             "login_url": "/auth/login" if oidc_settings is not None else "/",
                             "public_url": managed_settings.public_url if managed_settings else ""},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "knowledge-layer-evaluation"}

    @app.get("/api/ready")
    def ready() -> dict[str, str]:
        nonlocal schema_checked_at, schema_current
        try:
            if managed:
                # The ingress also forwards this unauthenticated probe. Keep the
                # expensive Alembic comparison off the per-request path, including
                # while a drifted database is failing readiness.
                if _monotonic() - schema_checked_at >= 30:
                    with schema_check_lock:
                        if _monotonic() - schema_checked_at >= 30:
                            from knowledge_layer.migrate import ensure_current_schema

                            try:
                                ensure_current_schema(knowledge_store)
                                schema_current = True
                            except (SQLAlchemyError, RuntimeError):
                                schema_current = False
                            schema_checked_at = _monotonic()
                if not schema_current:
                    raise HTTPException(status_code=503, detail="Knowledge registry schema is unavailable")
            with knowledge_store.engine.connect() as connection:
                connection.execute(sql_text("SELECT 1"))
        except (SQLAlchemyError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="Knowledge registry is unavailable") from exc
        return {"status": "ready"}

    agent = FastAPI(title="Knowledge Layer Agent API")
    agent.mount("/api", build_agent_api(
        knowledge_store, enable_source_management=source_management, allowed_kinds=allowed_kinds,
        durable_jobs=managed, enable_autonomy=autonomy,
    ))
    mcp_security = None
    if managed:
        public = urlparse(managed_settings.public_url)
        mcp_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=([public.netloc] if public.netloc else []) + ["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=([managed_settings.public_url] if public.netloc else []) + ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    agent.mount(
        "/mcp",
        McpEnvelopeBoundary(quick_server.streamable_http_app(
            streamable_http_path="/", json_response=True, stateless_http=True,
            transport_security=mcp_security,
        )),
    )
    agent.mount(
        "/advanced-mcp",
        McpEnvelopeBoundary(mcp_server.streamable_http_app(
            streamable_http_path="/", json_response=True, stateless_http=True,
            transport_security=mcp_security,
        )),
    )
    agent_boundary = (LocalAgentBoundary(agent, local_settings) if local_settings is not None else
                      agent if iap_settings is not None else
                      OidcAgentBoundary(agent, oidc_settings) if oidc_settings else AgentBearerBoundary(agent, token))
    app.mount("/agent", agent_boundary)
    web_dist = os.getenv("KNOWLEDGE_WEB_DIST")
    if web_dist and (Path(web_dist) / "index.html").is_file():
        static = StaticFiles(directory=web_dist, html=True)
        app.mount("/", BrowserStaticBoundary(static, oidc_settings) if oidc_settings else static, name="web")
    # This outer boundary bounds actual bytes before FastAPI JSON parsing and
    # OIDC/session processing. MCP retains its separate auth-first envelope cap.
    app.add_middleware(HttpRequestBudget)
    return app


app = create_platform()
