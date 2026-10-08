"""Actual bytes, rather than claimed lengths, bound every HTTP JSON request."""

import asyncio

import pytest

from knowledge_layer.request_budget import HttpRequestBudget


@pytest.mark.parametrize("headers,chunks,expected", [
    ([], [b"abcd", b"efghi"], 413),
    ([(b"content-length", b"9")], [b"unread"], 413),
    ([(b"content-length", b"1")], [b"ab"], 400),
    ([(b"content-length", b"2"), (b"content-length", b"2")], [b"ab"], 400),
    ([(b"content-length", b"-1")], [b"ab"], 400),
    ([(b"content-encoding", b"gzip")], [b"unread"], 415),
    ([], [b"ab", b"cd"], 200),
])
def test_http_budget_bounds_chunked_and_encoded_bodies_before_downstream(headers, chunks, expected):
    called, reads, messages = [], [], []
    async def app(scope, receive, send):
        called.append(True)
        assert (await receive())["body"] == b"abcd"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        index = len(reads)
        reads.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    async def send(message):
        messages.append(message)

    asyncio.run(HttpRequestBudget(app, max_bytes=8)({"type": "http", "headers": headers}, receive, send))
    assert messages[0]["status"] == expected
    assert bool(called) == (expected == 200)
    if expected == 415 or any(value == b"9" for _, value in headers):
        assert reads == []
