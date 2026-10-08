"""Pilot monitoring preserves denial/freshness distinctions and redacts failures."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import monitor_installation as monitor


def test_monitor_refuses_redirecting_or_non_https_targets():
    for target in ("http://remote.example.org", "https://user:password@example.org",
                   "https://example.org/private", "https://example.org?token=private"):
        with pytest.raises(ValueError):
            monitor.origin(target)
    assert monitor.origin("http://127.0.0.1:8273", True) == "http://127.0.0.1:8273"
    assert monitor.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example") is None


@pytest.mark.parametrize("clock,error", [
    (None, "complete_inventory_clock_unknown"),
    ((datetime.now(UTC) - timedelta(days=2)).isoformat(), "complete_inventory_outside_freshness_window"),
    (datetime.now(UTC).isoformat(), None),
])
def test_monitor_never_substitutes_attempt_clock_for_complete_inventory(tmp_path, monkeypatch, clock, error):
    token = tmp_path / "token"
    token.write_text("private-test-marker")
    token.chmod(0o600)
    def request(base, path, credential):
        if path == "/api/ready":
            return 200, {}
        if credential is None:
            return 401, {}
        assert credential == "private-test-marker"
        return 200, {"connections": [{"id": "conn_test", "status": "discovery_complete",
            "last_complete_refresh_at": clock, "last_refresh_attempt_at": datetime.now(UTC).isoformat()}]}
    monkeypatch.setattr(monitor, "request", request)
    if error:
        with pytest.raises(ValueError, match=error):
            monitor.sample("https://example.org", token, ["conn_test"], 86400)
    else:
        assert monitor.sample("https://example.org", token, ["conn_test"], 86400)["result"] == "passed"


def test_monitor_records_only_fixed_failure_code(tmp_path, monkeypatch, capsys):
    expected = tmp_path / "sources"
    expected.write_text(json.dumps(["conn_test"]))
    expected.chmod(0o600)
    def unavailable(*args):
        raise OSError("private-test-marker https://private.example token")
    monkeypatch.setattr(monitor, "sample", unavailable)
    output = tmp_path / "samples.jsonl"
    result = monitor.main(["--origin", "https://example.org", "--agent-token-file", str(tmp_path / "token"),
        "--expected-sources-file", str(expected), "--output", str(output)])
    assert result == 1
    assert "private-test-marker" not in output.read_text() + capsys.readouterr().out
    assert json.loads(output.read_text())["reason"] == "probe_failed"
    assert output.stat().st_mode & 0o077 == 0
