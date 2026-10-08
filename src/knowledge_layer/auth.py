"""Local-demo bearer boundary for the agent-facing knowledge endpoint.

This deliberately does not pretend to be enterprise identity. Production
deployment needs a tenant-aware identity provider, delegated user identity,
and source policy enforcement before remote data access is enabled.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse

from knowledge_layer.request_budget import close_request_headers

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]


class AgentBearerBoundary:
    """Require one configured token before allowing MCP/agent HTTP requests."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token or ""

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self.token:
            await JSONResponse({"detail": "Agent endpoint is disabled: token is not configured"}, status_code=503,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        supplied = next((value for key, value in scope.get("headers", []) if key.lower() == b"authorization"), b"")
        prefix = b"Bearer "
        candidate = supplied[len(prefix):].decode("utf-8", "ignore") if supplied.startswith(prefix) else ""
        if not hmac.compare_digest(candidate, self.token):
            await JSONResponse(
                {"detail": "A valid agent bearer token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", **close_request_headers(scope)},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)
