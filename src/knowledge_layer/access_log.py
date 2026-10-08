"""Bounded access events for an operator-owned log pipeline in OIDC mode."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from knowledge_layer.auth import ASGIApp

_SOURCE_ID = re.compile(r"conn_[A-Za-z0-9_-]{1,155}\Z")
_LOGGER = logging.getLogger("knowledge_layer.access")
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
if not _LOGGER.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)


def emit_access_event(event: dict[str, Any]) -> None:
    """Write one JSON object; the deployer owns delivery and retention."""
    _LOGGER.info(json.dumps(event, separators=(",", ":"), sort_keys=True))


@dataclass
class SourceReadTrace:
    """Mutable trace shared with sync route workers through copied context vars."""

    _sources: set[tuple[str, str]] = field(default_factory=set)
    _lock: Lock = field(default_factory=Lock)

    def record(self, tenant_id: str, connection_id: str) -> None:
        if isinstance(tenant_id, str) and isinstance(connection_id, str) and _SOURCE_ID.fullmatch(connection_id):
            with self._lock:
                self._sources.add((tenant_id, connection_id))

    def sources(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._sources)


_READ_TRACE: ContextVar[SourceReadTrace | None] = ContextVar("knowledge_source_reads", default=None)


def record_source_read(tenant_id: str, connection_id: str) -> None:
    """Record only an authorized source row or snapshot that was actually read."""
    trace = _READ_TRACE.get()
    if trace is not None:
        trace.record(tenant_id, connection_id)


@contextmanager
def source_read_trace():
    trace = SourceReadTrace()
    token = _READ_TRACE.set(trace)
    try:
        yield trace
    finally:
        _READ_TRACE.reset(token)


def _surface(path: str) -> str | None:
    if path.startswith("/api/knowledge/") or path == "/api/knowledge":
        return "browser"
    if path.startswith("/agent/api/") or path == "/agent/api":
        return "agent_http"
    if path.startswith("/agent/mcp/") or path == "/agent/mcp":
        return "agent_mcp"
    if path.startswith("/agent/advanced-mcp/") or path == "/agent/advanced-mcp":
        return "agent_advanced_mcp"
    return None


class AccessLogBoundary:
    """Log authenticated access outcome without capturing request or response bodies."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        if scope["type"] != "http" or (surface := _surface(scope.get("path", ""))) is None:
            await self.app(scope, receive, send)
            return
        status_code = 500

        async def capture_status(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        with source_read_trace() as trace:
            try:
                await self.app(scope, receive, capture_status)
            finally:
                actor = scope.get("knowledge_actor")
                if isinstance(actor, str) and actor and len(actor) <= 255:
                    endpoint = scope.get("endpoint")
                    operation = getattr(endpoint, "__name__", "request")
                    if surface.endswith("mcp"):
                        # Tool name and arguments are in the request body, which is
                        # deliberately never inspected by this access logger.
                        operation = "mcp_request"
                    params = scope.get("path_params")
                    source_id = params.get("connection_id") if isinstance(params, dict) else None
                    event = {
                        "event": "knowledge.access", "at": datetime.now(UTC).isoformat(),
                        "actor": actor, "surface": surface, "operation": operation,
                        "method": scope.get("method", ""), "status": status_code,
                    }
                    if isinstance(source_id, str) and _SOURCE_ID.fullmatch(source_id):
                        event["connection_id"] = source_id
                    emit_access_event(event)
                    for tenant_id, connection_id in trace.sources():
                        emit_access_event({
                            **event, "event": "knowledge.source_read",
                            "tenant_id": tenant_id, "connection_id": connection_id,
                        })
