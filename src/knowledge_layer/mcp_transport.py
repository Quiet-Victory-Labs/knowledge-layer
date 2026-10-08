"""Bound the authenticated MCP envelope before the SDK's header-only routing.

SDK 2.2 routes missing and handshake-version headers to its legacy dispatcher.
Modern body metadata must not bypass mirrored-header validation through it.
Other protocol validation remains owned by the SDK.
"""

from __future__ import annotations

import json

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from knowledge_layer.request_budget import (
    MAX_BODY_READ_SECONDS,
    RequestBodyTooLarge,
    close_request_headers,
    read_bounded_body,
    validate_body_timeout,
)

_HANDSHAKE_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})
_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
MAX_MCP_REQUEST_BYTES = 1024 * 1024


class McpEnvelopeBoundary:
    """Inspect bounded POST bodies after auth, replaying exact bytes to the SDK."""

    def __init__(self, app: ASGIApp, *, body_timeout_seconds: float = MAX_BODY_READ_SECONDS):
        self.app = app
        validate_body_timeout(body_timeout_seconds)
        self.body_timeout_seconds = body_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        try:
            payload = await read_bounded_body(
                receive, max_bytes=MAX_MCP_REQUEST_BYTES, timeout_seconds=self.body_timeout_seconds)
        except RequestBodyTooLarge:
            await JSONResponse({"detail": "MCP request exceeds the 1 MiB envelope limit"},
                               status_code=413, headers=close_request_headers(scope))(scope, receive, send)
            return
        except TimeoutError:
            await JSONResponse({"detail": "MCP request body deadline exceeded"},
                               status_code=408, headers=close_request_headers(scope))(scope, receive, send)
            return
        if payload is None:
            return
        try:
            envelope = json.loads(payload)
        except (ValueError, UnicodeError, RecursionError):
            envelope = None
        params = envelope.get("params") if isinstance(envelope, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(meta, dict) and _VERSION_KEY in meta:
            version = meta[_VERSION_KEY]
            if not isinstance(version, str) or version not in _HANDSHAKE_VERSIONS:
                headers = [value.decode("latin-1") for key, value in scope.get("headers", [])
                           if key.lower() == b"mcp-protocol-version"]
                if headers != [version]:
                    request_id = envelope.get("id")
                    if type(request_id) not in (str, int):
                        request_id = None
                    await JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {
                        "code": -32020, "message": "MCP-Protocol-Version must match request metadata"}},
                        status_code=400, headers=close_request_headers(scope))(scope, receive, send)
                    return
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
