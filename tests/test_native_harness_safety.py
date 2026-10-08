"""Native harness safety gates without model calls or network services."""

from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import os
import signal
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "drill_native_harness.py"
spec = importlib.util.spec_from_file_location("native_harness_safety", SCRIPT)
assert spec and spec.loader
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_new_report_is_private_regular_cloexec_file(tmp_path):
    path = tmp_path / "report.json"
    with drill._open_report(path) as report:
        observed = os.fstat(report.fileno())
        assert stat.S_ISREG(observed.st_mode)
        assert observed.st_mode & 0o777 == 0o600
        assert observed.st_uid == os.geteuid() and observed.st_nlink == 1
        assert fcntl.fcntl(report.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        json.dump({"result": "fixture"}, report)
    assert json.loads(path.read_text()) == {"result": "fixture"}


@pytest.mark.parametrize("unsafe", ["symlink", "public", "hardlink"])
def test_unsafe_report_refuses_before_truncating_original(tmp_path, unsafe):
    target = tmp_path / "preserved.json"
    original = '{"existing": "preserve this exact value"}\n'
    target.write_text(original)
    target.chmod(0o644 if unsafe == "public" else 0o600)
    destination = target
    if unsafe == "symlink":
        destination = tmp_path / "symlink.json"
        destination.symlink_to(target)
    elif unsafe == "hardlink":
        destination = tmp_path / "hardlink.json"
        os.link(target, destination)
    with pytest.raises((OSError, drill.DrillFailure)), drill._open_report(destination):
        pytest.fail("Unsafe report target was opened for writing")
    assert target.read_text() == original


def test_nonowned_report_refuses_before_truncate(tmp_path, monkeypatch):
    target = tmp_path / "preserved.json"
    target.write_text("preserved contents")
    target.chmod(0o600)
    original_fstat = os.fstat

    def foreign_owner(fd):
        fields = list(original_fstat(fd))
        fields[4] = os.geteuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(drill.os, "fstat", foreign_owner)
    with pytest.raises((OSError, drill.DrillFailure)), drill._open_report(target):
        pytest.fail("A foreign-owned target was opened for writing")
    assert target.read_text() == "preserved contents"


@pytest.mark.skipif(os.name != "posix", reason="FIFO and nonblocking flags require POSIX")
def test_nonregular_fifo_report_refuses_without_a_blocking_open(tmp_path, monkeypatch):
    path = tmp_path / "report-pipe"
    os.mkfifo(path, 0o600)
    original_open = os.open
    reader = original_open(path, os.O_RDONLY | os.O_NONBLOCK)
    flags_seen = []

    def open_report_path(target, flags, *args, **kwargs):
        if Path(target) == path:
            flags_seen.append(flags)
            assert flags & os.O_NONBLOCK
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(drill.os, "open", open_report_path)
    try:
        with pytest.raises((OSError, drill.DrillFailure)), drill._open_report(path):
            pytest.fail("A nonregular report target was opened for writing")
    finally:
        os.close(reader)
    assert flags_seen and stat.S_ISFIFO(path.stat().st_mode)


def test_unsafe_destination_preflight_precedes_model_execution(tmp_path, monkeypatch, capsys):
    target = tmp_path / "preserved.json"
    target.write_text("unchanged")
    target.chmod(0o600)
    output = tmp_path / "report.json"
    output.symlink_to(target)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--client", "codex", "--output", str(output)])

    def no_models(*_args):
        pytest.fail("Model execution started before report destination preflight")

    monkeypatch.setattr(drill, "run_client", no_models)
    assert drill.main() == 1
    assert target.read_text() == "unchanged"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert str(target) not in captured.out and str(output) not in captured.out


def _mock_process_group(monkeypatch, *, persists):
    signals = []
    alive = True

    def killpg(pid, sig):
        nonlocal alive
        assert pid == 987654321
        if not alive:
            raise ProcessLookupError
        if sig:
            signals.append(sig)
        if sig == signal.SIGKILL and not persists:
            alive = False

    ticks = 0.0

    def monotonic():
        nonlocal ticks
        ticks += 1
        return ticks

    async def sleep(_seconds):
        return None

    async def wait():
        return 0

    monkeypatch.setattr(drill.os, "killpg", killpg)
    monkeypatch.setattr(drill, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(drill.asyncio, "sleep", sleep)
    process = SimpleNamespace(pid=987654321, returncode=0, wait=wait)
    return process, signals


def test_cleanup_escalates_even_when_process_leader_already_exited(monkeypatch):
    process, signals = _mock_process_group(monkeypatch, persists=False)
    asyncio.run(drill._stop_process_group(process))
    assert signal.SIGTERM in signals and signal.SIGKILL in signals


def test_persistent_process_group_fails_closed_after_kill(monkeypatch):
    process, signals = _mock_process_group(monkeypatch, persists=True)
    with pytest.raises(drill.DrillFailure, match="^cli_cleanup$"):
        asyncio.run(drill._stop_process_group(process))
    assert signal.SIGTERM in signals and signal.SIGKILL in signals


def test_group_inspection_permission_failure_is_sanitized(monkeypatch):
    async def wait():
        return 0

    def deny(*_args):
        raise PermissionError("private process diagnostic must not escape")

    monkeypatch.setattr(drill.os, "killpg", deny)
    process = SimpleNamespace(pid=987654321, returncode=0, wait=wait)
    with pytest.raises(drill.DrillFailure, match="^cli_cleanup$"):
        asyncio.run(drill._stop_process_group(process))


@pytest.mark.parametrize("surface,tool", [
    ("/agent/mcp", "propose_meaning"),
    ("/source/mcp", "get_workspace"),
    ("/agent/mcp", "execute_reviewed_plan"),
])
def test_out_of_scope_tool_is_refused_before_app_runs_and_fault_is_sticky(surface, tool):
    observer = drill.Observer()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": {}}}).encode()
    messages = []

    async def forbidden_app(*_args):
        pytest.fail("An out-of-scope MCP request reached the actual server")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(observer.wrap(forbidden_app, surface)(
        {"type": "http", "method": "POST"}, receive, send))
    assert observer.failure == "unexpected_tool"
    assert next(message["status"] for message in messages if message["type"] == "http.response.start") == 400
    assert observer.events == [] and observer.plans == {} and observer.validated == set()
    observer.record("/agent/mcp", {"method": "tools/call", "params": {"name": "get_workspace"}},
                    {"result": {"structuredContent": {}}})
    assert observer.failure == "unexpected_tool"


def test_observer_app_exception_remains_failed_after_a_later_success():
    observer = drill.Observer()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_workspace", "arguments": {}}}).encode()

    async def broken_app(*_args):
        raise RuntimeError("fixture observer interrupted")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(_message):
        return None

    with pytest.raises(RuntimeError, match="fixture observer interrupted"):
        asyncio.run(observer.wrap(broken_app, "/agent/mcp")(
            {"type": "http", "method": "POST"}, receive, send))
    observer.record("/agent/mcp", {"method": "tools/call", "params": {"name": "get_workspace"}},
                    {"result": {"structuredContent": {}}})
    assert observer.failure == "observer_failed"


def test_denial_evidence_keeps_exact_argument_digest():
    observer = drill.Observer()
    captured_receipt = {"opaque": "actual previous fixture receipt"}
    wrong_receipt = {"opaque": "wrong fixture receipt"}
    args = {"receipt": wrong_receipt}
    observer.record("/agent/mcp", {"method": "tools/call", "params": {
        "name": "revalidate_task_context", "arguments": args}}, {"result": {"isError": True}})
    assert ("revalidate_task_context", drill._digest(args)) in observer.denied
    assert ("revalidate_task_context", drill._digest({"receipt": captured_receipt})) not in observer.denied
    assert observer.validated == set()


def test_non_tool_wire_events_have_an_absolute_budget():
    observer = drill.Observer()
    with pytest.raises(drill.DrillFailure, match="^observation_budget$"):
        for _ in range(501):
            observer.record("/agent/mcp", {"method": "tools/list", "params": {}}, {"result": {}})
    assert observer.failure == "observation_budget"
    assert len(observer.wire_events) <= 500


@pytest.mark.skipif(os.name != "posix", reason="Native drill owns POSIX process groups")
def test_real_orphaned_child_ignoring_term_is_removed_after_leader_exits():
    async def exercise():
        child_code = ("import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                      "print(os.getpid(),flush=True);os.close(1);time.sleep(30)")
        parent_code = ("import subprocess,sys;"
                       "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]],"
                       "stdout=subprocess.PIPE,stderr=subprocess.DEVNULL);"
                       "print(child.stdout.readline().decode().strip(),flush=True);child.stdout.close()")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent_code, child_code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 5)
            child_pid = int(line.strip())
            await asyncio.wait_for(process.wait(), 5)
            assert process.returncode == 0 and os.getpgid(child_pid) == process.pid
            await asyncio.wait_for(drill._stop_process_group(process), 15)
            with pytest.raises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), 5)

    asyncio.run(exercise())
