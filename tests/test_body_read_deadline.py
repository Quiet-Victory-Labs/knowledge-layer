"""Slow request bodies expire without timing downstream MCP processing."""

import asyncio

import pytest

from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.mcp_transport import McpEnvelopeBoundary
from knowledge_layer.request_budget import HttpRequestBudget, read_bounded_body


@pytest.mark.parametrize("boundary", [HttpRequestBudget, McpEnvelopeBoundary])
@pytest.mark.parametrize("dripping", [False, True])
def test_stalled_and_dripping_body_has_total_deadline_before_downstream(boundary, dripping):
    called, messages, reads, cancelled = [], [], [], []

    async def app(scope, receive, send):
        called.append(True)

    async def receive():
        reads.append(True)
        try:
            await asyncio.sleep(0.005 if dripping else 3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return {"type": "http.request", "body": b"x", "more_body": True}

    async def send(message):
        messages.append(message)

    async def run():
        await asyncio.wait_for(boundary(app, body_timeout_seconds=0.05)(
            {"type": "http", "method": "POST", "path": "/api/health", "headers": []},
            receive, send), timeout=2)

    asyncio.run(run())
    assert messages[0]["status"] == 408
    assert (b"connection", b"close") in messages[0]["headers"]
    assert called == [] and cancelled == [True]
    assert len(reads) >= (2 if dripping else 1)


@pytest.mark.parametrize("boundary", [HttpRequestBudget, McpEnvelopeBoundary])
def test_body_deadline_does_not_time_downstream_processing(boundary):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def app(scope, replay, send):
        assert (await replay())["body"] == b"{}"
        await asyncio.sleep(0.03)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        messages.append(message)

    asyncio.run(boundary(app, body_timeout_seconds=0.01)(
        {"type": "http", "method": "POST", "path": "/api/health", "headers": []}, receive, send))
    assert messages[0]["status"] == 200


def test_mcp_unauthorized_stalled_body_is_denied_without_reading():
    messages = []

    async def app(scope, receive, send):
        pytest.fail("Unauthorized MCP request reached downstream")

    async def receive():
        pytest.fail("Unauthorized MCP body was read")

    async def send(message):
        messages.append(message)

    boundary = HttpRequestBudget(AgentBearerBoundary(McpEnvelopeBoundary(app), "offline-agent"))
    asyncio.run(boundary({"type": "http", "method": "POST", "path": "/agent/mcp/",
                          "headers": []}, receive, send))
    assert messages[0]["status"] == 401
    assert (b"connection", b"close") in messages[0]["headers"]


@pytest.mark.parametrize("boundary", [HttpRequestBudget, McpEnvelopeBoundary])
def test_http2_body_refusal_omits_forbidden_connection_header(boundary):
    messages = []

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    asyncio.run(boundary(None, body_timeout_seconds=0.01)(
        {"type": "http", "method": "POST", "path": "/api/health", "headers": [],
         "http_version": "2"}, receive, send))
    assert messages[0]["status"] == 408
    assert not any(key == b"connection" for key, _ in messages[0]["headers"])


def test_many_tiny_chunks_replay_exact_bytes_and_disconnect_refuses_partial_body():
    async def run():
        count = 0

        async def receive():
            nonlocal count
            count += 1
            return {"type": "http.request", "body": b"x", "more_body": count < 20_000}

        assert await read_bounded_body(receive, max_bytes=20_000, timeout_seconds=1) == b"x" * 20_000

        async def disconnected():
            return {"type": "http.disconnect"}

        assert await read_bounded_body(disconnected, max_bytes=8, timeout_seconds=1) is None

    asyncio.run(run())


@pytest.mark.parametrize("seconds", [0, -1, 11, float("inf"), float("nan")])
@pytest.mark.parametrize("boundary", [HttpRequestBudget, McpEnvelopeBoundary])
def test_body_deadline_cannot_be_disabled_or_exceed_cap(boundary, seconds):
    with pytest.raises(ValueError):
        boundary(None, body_timeout_seconds=seconds)
