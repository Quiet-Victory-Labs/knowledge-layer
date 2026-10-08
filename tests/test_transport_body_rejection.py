"""Rejecting an unfinished upload must release its HTTP/1 connection promptly."""

import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn

from knowledge_layer.auth import AgentBearerBoundary
from knowledge_layer.request_budget import HttpRequestBudget


@contextmanager
def _loopback_server(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    address = listener.getsockname()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, http="httptools", loop="asyncio",
        lifespan="off", access_log=False, log_level="critical",
        timeout_keep_alive=5, timeout_graceful_shutdown=1,
    ))
    errors = []

    def run():
        try:
            server.run(sockets=[listener])
        except BaseException as exc:  # noqa: BLE001 - Report server-thread failures to the test.
            errors.append(exc)

    thread = threading.Thread(target=run, name="body-rejection-test", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not errors, errors
        assert server.started, "Loopback transport did not start"
        yield address
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=3)
        listener.close()
        assert not thread.is_alive(), "Loopback transport did not shut down"
        assert not errors, errors


def _read_response(client):
    response = bytearray()
    while b"\r\n\r\n" not in response:
        data = client.recv(4096)
        assert data, "Peer closed before sending response headers"
        response.extend(data)
    headers, _, body = bytes(response).partition(b"\r\n\r\n")
    content_length = next(
        int(line.split(b":", 1)[1].strip())
        for line in headers.split(b"\r\n")[1:]
        if line.lower().startswith(b"content-length:")
    )
    while len(body) < content_length:
        data = client.recv(4096)
        assert data, "Peer closed before completing the rejection response"
        body += data
    assert len(body) == content_length
    return int(headers.split(b" ", 2)[1])


@pytest.mark.parametrize("boundary,upload,late_body,expected", [
    (
        "budget",
        (
            b"POST /api/example HTTP/1.1\r\nHost: localhost\r\n"
            b"Connection: keep-alive\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n"  # Omit the terminating chunk from this valid chunked upload.
        ),
        b"1\r\nx\r\n",
        413,
    ),
    (
        "bearer",
        (
            b"POST /agent/api/example HTTP/1.1\r\nHost: localhost\r\n"
            b"Connection: keep-alive\r\nAuthorization: Bearer wrong\r\n"
            b"Content-Length: 100\r\n\r\nx"
        ),
        b"x",
        401,
    ),
    (
        "disabled",
        (
            b"POST /agent/api/example HTTP/1.1\r\nHost: localhost\r\n"
            b"Connection: keep-alive\r\nContent-Length: 100\r\n\r\nx"
        ),
        b"x",
        503,
    ),
])
def test_partial_upload_rejection_closes_transport(boundary, upload, late_body, expected):
    called = []

    async def downstream(scope, receive, send):
        called.append(True)
        raise AssertionError("Rejected upload reached the application")

    app = HttpRequestBudget(downstream, max_bytes=4)
    if boundary != "budget":
        app = AgentBearerBoundary(app, "valid" if boundary == "bearer" else None)

    with (
        _loopback_server(app) as address,
        socket.create_connection(address, timeout=2) as client,
    ):
        client.settimeout(1)
        client.sendall(upload)
        assert _read_response(client) == expected
        # Incoming data cancels Uvicorn's idle keepalive timer. Rejected
        # connections therefore need an explicit close, not that timer.
        try:
            client.sendall(late_body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            assert client.recv(1) == b"", "Rejected upload connection stayed open"
        except ConnectionResetError:
            pass  # A reset also releases the rejected transport.
        except TimeoutError as exc:
            raise AssertionError("Rejected upload connection stayed open") from exc
    assert not called
