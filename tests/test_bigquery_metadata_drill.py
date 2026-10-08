"""The live BigQuery drill mints a bounded, read-only token without a key."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scripts import drill_bigquery_metadata as drill


def _expiry(seconds: int = 600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _client(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> httpx.Client:
    def caller(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert args == ["gcloud", "auth", "print-access-token", "--quiet"]
        assert kwargs == {"capture_output": True, "check": True, "timeout": 45}
        return subprocess.CompletedProcess(args, 0, b"operator-token-value-123456\n")

    monkeypatch.setattr(drill.subprocess, "run", caller)
    return httpx.Client(transport=httpx.MockTransport(lambda request: _reply(request, response)))


def _reply(request: httpx.Request, response: httpx.Response) -> httpx.Response:
    assert request.method == "POST"
    assert str(request.url) == (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        "reader@example-project.iam.gserviceaccount.com:generateAccessToken"
    )
    assert request.headers["Authorization"] == "Bearer operator-token-value-123456"
    assert request.headers["X-Goog-User-Project"] == "example-project"
    assert json.loads(request.content) == {
        "scope": ["https://www.googleapis.com/auth/bigquery.readonly"], "lifetime": "600s",
    }
    return response


def test_scoped_impersonation_uses_iam_credentials_api(monkeypatch: pytest.MonkeyPatch):
    response = httpx.Response(200, json={"accessToken": "scoped-token-value-123456", "expireTime": _expiry()})
    with _client(monkeypatch, response) as client:
        token, expiry = drill._scoped_access_token("reader@example-project.iam.gserviceaccount.com",
            "example-project", client)
    assert token == "scoped-token-value-123456"
    assert 60 < (expiry - datetime.now(UTC)).total_seconds() <= 600


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": "https://elsewhere.example"}),
    httpx.Response(200, content=b"x" * 16_385),
    httpx.Response(200, json={"accessToken": "scoped-token-value-123456", "expireTime": "not-a-date"}),
    httpx.Response(200, json={"accessToken": "bad\nheader", "expireTime": _expiry()}),
    httpx.Response(200, json={"accessToken": "scoped-token-value-123456", "expireTime": _expiry(3700)}),
])
def test_scoped_impersonation_rejects_bad_responses(monkeypatch: pytest.MonkeyPatch, response: httpx.Response):
    with _client(monkeypatch, response) as client, pytest.raises(ValueError):
        drill._scoped_access_token("reader@example-project.iam.gserviceaccount.com", "example-project", client)
