"""Release qualification refuses credential forwarding and preserves failed cleanup."""
from __future__ import annotations

import errno
import json
import shutil
import socket
import subprocess
import sys
from urllib.request import Request

import pytest

from scripts import drill_self_host as drill
from scripts import self_host


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_free_port_probe_refuses_existing_listener_without_disrupting_it(host):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, 0))
        listener.listen(2)
        listener.settimeout(2)
        port = listener.getsockname()[1]
        with pytest.raises(self_host.OperationError, match="occupied"):
            self_host.require_free_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            # The listener remains alive and under the original caller's control.
            first, _ = listener.accept()
            first.close()
            second, _ = listener.accept()
            second.close()


def test_free_port_probe_allows_restart_after_closed_http_connection():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            server, _ = listener.accept()
            server.shutdown(socket.SHUT_WR)
            server.close()  # Active close puts the old server connection in TIME_WAIT.
            assert client.recv(1) == b""
    self_host.require_free_port(port)


def test_unknown_loopback_probe_status_refuses_before_binding(monkeypatch):
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def settimeout(self, *_):
            pass
        def connect_ex(self, *_):
            return errno.ETIMEDOUT
        def bind(self, *_):
            pytest.fail("Unknown port state must not bind")
    monkeypatch.setattr(self_host.socket, "socket", lambda *_: Probe())
    with pytest.raises(self_host.OperationError):
        self_host.require_free_port(4427)


def test_unbound_ipv6_http_origin_is_refused_before_installation(tmp_path):
    path = tmp_path / "install"
    with pytest.raises(ValueError, match="ipv4_loopback"):
        self_host.initialize(path, public_url="http://[::1]:4427", port=4427)
    assert not path.exists()


@pytest.mark.parametrize("client", ["codex", "claude", "cursor"])
def test_all_client_handoffs_work_with_isolated_stdlib_only_host_python(tmp_path, client):
    install, project = tmp_path / "install", tmp_path / "project"
    self_host.initialize(install, port=4427)
    project.mkdir()
    result = subprocess.run([sys.executable, "-I", "-S", str(self_host.ROOT / "scripts/self_host.py"),
        "--install-dir", str(install), "agent", "--client", client, "--project", str(project)],
        capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    token = (install / "secrets/agent_token").read_text()
    assert token not in result.stdout + result.stderr
    assert any(path.is_file() for path in project.rglob("*"))


def test_private_client_never_follows_redirects_or_ambient_proxies():
    request = Request("http://127.0.0.1:4000/auth", headers={"Authorization": "Bearer private"})
    assert drill.RefuseRedirect().redirect_request(request, None, 307, "redirect", {}, "https://other.test") is None
    client = drill.Client("http://127.0.0.1:4000")
    assert not any(getattr(handler, "proxies", {}) for handler in client.opener.handlers)
    for path in ("https://other.test", "//other.test", "relative"):
        with pytest.raises(ValueError, match="relative"):
            client.request(path)


@pytest.mark.parametrize("raw", [b"not-json", b"{}" * drill.MAX_RESPONSE])
def test_private_http_response_budget_and_invalid_json_are_refused(raw):
    class Response:
        status = 200
        def read(self, limit):
            return raw[:limit]
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    client = drill.Client("http://127.0.0.1:4000")
    client.opener.open = lambda *_args, **_kwargs: Response()
    with pytest.raises((ValueError, RuntimeError)):
        client.request("/agent/mcp/")


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_start_failure_removes_owned_project_or_preserves_private_retry_files(monkeypatch, tmp_path, cleanup_fails):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(drill.tempfile, "mkdtemp", lambda **_: str(private))
    calls = []
    def command(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": "sha256:" + "a" * 64}])
        return ""
    monkeypatch.setattr(drill, "command", command)
    def cli(install, operation, *args):
        if operation == "init":
            install.mkdir(mode=0o700)
            (install / "sources").mkdir(mode=0o700)
            (install / "installation.json").write_text(json.dumps({"project": "knowledge-layer-" + "b" * 12}))
        elif operation == "start":
            raise RuntimeError("private-start-canary")
        elif operation == "remove":
            if cleanup_fails:
                raise RuntimeError("private-cleanup-canary")
            shutil.rmtree(install)
    monkeypatch.setattr(drill, "cli", cli)
    report = {"stage": "preflight", "checks": []}
    with pytest.raises(RuntimeError):
        drill.qualify("prebuilt", report)
    assert private.exists() is cleanup_fails
    assert report["checks"][-1]["passed"] is not cleanup_fails
    assert report["stage_before_cleanup"] == "start"
    assert ("private_cleanup_directory" in report) is cleanup_fails
    assert all("unrelated" not in arg for call in calls for arg in call)


def test_failed_drill_and_driver_messages_are_redacted(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(drill, "command", lambda *_args, **_: "a" * 40)
    def failure(*_args):
        raise subprocess.SubprocessError("private-driver-canary")
    monkeypatch.setattr(drill, "qualify", failure)
    output = tmp_path / "result.json"
    assert drill.main(["--image", "prebuilt", "--postgres-image", "prebuilt-db", "--output", str(output)]) == 1
    emitted = capsys.readouterr().out
    assert "private-driver-canary" not in emitted + output.read_text()
    assert json.loads(output.read_text())["result"] == "failed"
    assert output.stat().st_mode & 0o077 == 0


def test_report_never_overwrites_existing_file_or_symlink(tmp_path):
    path = tmp_path / "result.json"
    path.write_text("preserved")
    with pytest.raises(FileExistsError):
        drill.write_report(path, {})
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        drill.write_report(link, {})
    assert path.read_text() == "preserved"
