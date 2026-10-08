"""Actual-byte HTTP request budget before JSON parsing or authentication work."""

from __future__ import annotations

import asyncio
import math

from starlette.responses import JSONResponse

MAX_HTTP_BODY_BYTES = 1024 * 1024
MAX_BODY_READ_SECONDS = 10.0


class RequestBodyTooLarge(ValueError):
    """Actual request bytes exceeded the caller's limit."""


async def read_bounded_body(receive, *, max_bytes: int, timeout_seconds: float) -> bytes | None:
    """Bound total reception time and storage, including many tiny chunks."""
    buffered = bytearray()
    async with asyncio.timeout(timeout_seconds):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            body = message.get("body", b"")
            if len(buffered) + len(body) > max_bytes:
                raise RequestBodyTooLarge
            buffered.extend(body)
            if not message.get("more_body", False):
                return bytes(buffered)


def validate_body_timeout(seconds: float) -> None:
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_BODY_READ_SECONDS:
        raise ValueError("Body read deadline exceeds the platform limit")


def close_request_headers(scope) -> dict[str, str]:
    # An early response may leave unread bytes. Close HTTP/1 sockets so a
    # rejected slow upload cannot retain a transport admission slot. HTTP/2
    # forbids this hop-by-hop header; its ingress must apply stream budgets.
    return {"Connection": "close"} if scope.get("http_version", "1.1") in {"1.0", "1.1"} else {}


class HttpRequestBudget:
    def __init__(self, app, *, max_bytes: int = MAX_HTTP_BODY_BYTES,
                 body_timeout_seconds: float = MAX_BODY_READ_SECONDS):
        if not 1 <= max_bytes <= MAX_HTTP_BODY_BYTES:
            raise ValueError("HTTP body budget exceeds the platform limit")
        self.app = app
        self.max_bytes = max_bytes
        validate_body_timeout(body_timeout_seconds)
        self.body_timeout_seconds = body_timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") in {
            "/agent/mcp", "/agent/advanced-mcp",
        }:
            # MCP already enforces an actual-byte envelope after its own
            # authentication boundary; preserve that denial-first contract.
            await self.app(scope, receive, send)
            return
        headers = [(key.lower(), value) for key, value in scope.get("headers", [])]
        lengths = [value for key, value in headers if key == b"content-length"]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 12:
                await JSONResponse({"detail": "Invalid request length"}, status_code=400,
                                   headers=close_request_headers(scope))(scope, receive, send)
                return
            if int(lengths[0]) > self.max_bytes:
                await JSONResponse({"detail": "Request body exceeds the size limit"}, status_code=413,
                                   headers=close_request_headers(scope))(scope, receive, send)
                return
        # Request payloads are JSON, never compressed upload streams. Refuse
        # encodings before any downstream middleware or parser can expand them.
        if any(value.strip().lower() not in (b"", b"identity") for key, value in headers if key == b"content-encoding"):
            await JSONResponse({"detail": "Compressed request bodies are not supported"}, status_code=415,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        try:
            buffered = await read_bounded_body(
                receive, max_bytes=self.max_bytes, timeout_seconds=self.body_timeout_seconds)
        except RequestBodyTooLarge:
            await JSONResponse({"detail": "Request body exceeds the size limit"}, status_code=413,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        except TimeoutError:
            await JSONResponse({"detail": "Request body deadline exceeded"}, status_code=408,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        if buffered is None:
            return
        if lengths and len(buffered) != int(lengths[0]):
            await JSONResponse({"detail": "Request length does not match the body"}, status_code=400,
                               headers=close_request_headers(scope))(scope, receive, send)
            return
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
