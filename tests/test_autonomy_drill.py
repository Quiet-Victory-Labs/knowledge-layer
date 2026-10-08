"""Offline drill safety checks; every Docker and database operation is mocked."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import StatementError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TARGET_ENV = ("DATABASE_URL", "DATABASE_URL_FILE", "PGHOST", "PGHOSTADDR", "PGPORT",
              "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")
PRIVATE_MARKER = "fixture-private-diagnostic-do-not-emit"


@pytest.fixture
def drill(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for key in TARGET_ENV:
        monkeypatch.delenv(key, raising=False)
    spec = importlib.util.spec_from_file_location("autonomy_drill_safety", SCRIPTS / "drill_autonomy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def no_subprocess(*_args, **_kwargs):
        pytest.fail("An offline safety test attempted an unmocked external process")

    monkeypatch.setattr(module.subprocess, "run", no_subprocess)
    return module


def _argv(monkeypatch, output, *options):
    monkeypatch.setattr("sys.argv", ["drill_autonomy.py", "--output", str(output), *options])


@pytest.mark.parametrize("managed", [False, True])
def test_inventory_qualification_runs_before_autonomy_in_one_runtime(
    drill, monkeypatch, tmp_path, managed,
):
    output, events = tmp_path / "report.json", []
    _argv(monkeypatch, output, "--include-inventory", *( ["--managed"] if managed else []))

    @contextmanager
    def runtime():
        events.append("runtime.enter")
        try:
            yield
        finally:
            events.append("runtime.cleaned")

    def inventory(report):
        events.append("inventory")
        report["checks"].append({"name": "fixture.inventory", "passed": True})

    def autonomy(report, selected_output):
        assert selected_output == output
        assert report["inventory_qualification"]["result"] == "passed"
        events.append("autonomy")
        report["checks"].append({"name": "fixture.autonomy", "passed": True})

    monkeypatch.setattr(drill, "disposable_database", runtime)
    monkeypatch.setattr(drill, "qualify_inventory", inventory)
    monkeypatch.setattr(drill, "qualify", autonomy)
    assert drill.main() == 0
    assert events == (["runtime.enter", "inventory", "autonomy", "runtime.cleaned"]
                      if managed else ["inventory", "autonomy"])
    report = json.loads(output.read_text())
    assert report["total_checks"] == report["inventory_qualification"]["total_checks"] == 1


def test_failed_inventory_stops_autonomy_and_cleans_owned_runtime(
    drill, monkeypatch, tmp_path, capsys,
):
    output, cleaned = tmp_path / "report.json", []
    _argv(monkeypatch, output, "--managed", "--include-inventory")

    @contextmanager
    def runtime():
        try:
            yield
        finally:
            cleaned.append(True)

    def fail(report):
        report["checks"].append({"name": "fixture.inventory", "passed": False})
        raise RuntimeError(PRIVATE_MARKER)

    monkeypatch.setattr(drill, "disposable_database", runtime)
    monkeypatch.setattr(drill, "qualify_inventory", fail)
    monkeypatch.setattr(drill, "qualify", lambda *_args: pytest.fail("Autonomy ran after inventory failure"))
    assert drill.main() == 1
    assert cleaned == [True]
    report = json.loads(output.read_text())
    assert report["result"] == report["inventory_qualification"]["result"] == "failed"
    console = capsys.readouterr().out
    assert PRIVATE_MARKER not in output.read_text() + console
    assert "fixture.inventory" not in console
    assert json.loads(console)["phases"] == {
        "inventory_qualification": {"checks": 1, "failed_indexes": [0]}}


def test_driver_failure_emits_only_safe_type_and_private_report(drill, monkeypatch, tmp_path, capsys):
    output = tmp_path / "report.json"
    _argv(monkeypatch, output)

    def fail(*_args):
        raise StatementError(PRIVATE_MARKER, "CREATE ROLE fixture_reader PASSWORD 'private-fixture-value'",
                             None, RuntimeError(PRIVATE_MARKER))

    monkeypatch.setattr(drill, "qualify", fail)
    assert drill.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert PRIVATE_MARKER not in captured.out and "private-fixture-value" not in captured.out
    result = json.loads(output.read_text())
    assert result["result"] == "failed" and result["error_type"] == "StatementError"
    assert PRIVATE_MARKER not in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600


def test_report_write_failure_is_sanitized(drill, monkeypatch, tmp_path, capsys):
    _argv(monkeypatch, tmp_path / "report.json")
    monkeypatch.setattr(drill, "qualify", lambda *_args: None)

    def refuse(*_args):
        raise PermissionError(PRIVATE_MARKER)

    monkeypatch.setattr(drill, "_write_report", refuse)
    assert drill.main() == 1
    captured = capsys.readouterr()
    assert captured.err == "" and PRIVATE_MARKER not in captured.out
    assert json.loads(captured.out) == {"result": "failed", "error_type": "ReportWriteFailure"}


@pytest.mark.parametrize("unsafe", ["symlink", "public_permissions"])
def test_unsafe_report_destination_refuses_without_overwriting(drill, monkeypatch, tmp_path, capsys, unsafe):
    target = tmp_path / "existing.json"
    target.write_text('{"preserved": true}')
    target.chmod(0o600 if unsafe == "symlink" else 0o644)
    output = tmp_path / "report.json" if unsafe == "symlink" else target
    if unsafe == "symlink":
        output.symlink_to(target)
    _argv(monkeypatch, output)
    monkeypatch.setattr(drill, "qualify", lambda *_args: None)
    assert drill.main() == 1
    assert target.read_text() == '{"preserved": true}'
    assert json.loads(capsys.readouterr().out)["error_type"] == "ReportWriteFailure"


@pytest.mark.parametrize("key", ["DATABASE_URL", "DATABASE_URL_FILE"])
def test_managed_refuses_ambient_target_before_launch(drill, monkeypatch, tmp_path, capsys, key):
    monkeypatch.setenv(key, PRIVATE_MARKER)
    _argv(monkeypatch, tmp_path / "report.json", "--managed")

    def no_qualification(*_args):
        pytest.fail("Managed mode borrowed an ambient database target")

    monkeypatch.setattr(drill, "qualify", no_qualification)
    assert drill.main() == 1
    captured = capsys.readouterr()
    assert captured.err == "" and PRIVATE_MARKER not in captured.out
    assert os.environ[key] == PRIVATE_MARKER


@pytest.mark.parametrize("suffix", ["?dbname=customer_registry", "?hostaddr=192.0.2.1",
                                     "?port=5439", "?options=-csearch_path=customer"])
def test_reused_disposable_guard_rejects_connection_overrides(drill, monkeypatch, tmp_path, suffix):
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://fixture_owner@127.0.0.1:55445/knowledge_build_fixture" + suffix)
    secret.chmod(0o600)
    monkeypatch.setenv("DATABASE_URL_FILE", str(secret))
    with pytest.raises(RuntimeError, match="disposable_loopback"):
        drill._database_url()


@pytest.mark.parametrize("key", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS"])
def test_reused_disposable_guard_rejects_ambient_libpq_target(drill, monkeypatch, tmp_path, key):
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://fixture_owner@127.0.0.1:55445/knowledge_build_fixture")
    secret.chmod(0o600)
    monkeypatch.setenv("DATABASE_URL_FILE", str(secret))
    monkeypatch.setenv(key, PRIVATE_MARKER)
    with pytest.raises(RuntimeError, match="disposable_loopback"):
        drill._database_url()


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def bind(self, address):
        assert address == ("127.0.0.1", 0)

    def getsockname(self):
        return "127.0.0.1", 55445


def _managed_mock(drill, monkeypatch, *, failure=None, label_matches=True):
    calls = []
    stopped_queries = 0
    monkeypatch.setattr(drill.socket, "socket", _Socket)

    def run(command, **_kwargs):
        nonlocal stopped_queries
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            if failure == "launch_timeout":
                raise subprocess.TimeoutExpired(command, 45, stderr=PRIVATE_MARKER)
        elif command[:2] == ["docker", "exec"]:
            assert command[3:6] == ["pg_isready", "-h", "127.0.0.1"]
            if failure == "readiness_timeout":
                raise subprocess.TimeoutExpired(command, 5, stderr=PRIVATE_MARKER)
        elif command[:2] == ["docker", "inspect"]:
            if failure == "inspect_failure":
                return SimpleNamespace(returncode=1, stdout="")
            label = command[-1] if label_matches else "an-unrelated-owner"
            return SimpleNamespace(returncode=0, stdout=json.dumps({"knowledge-layer.autonomy-drill": label}))
        elif command[:3] == ["docker", "container", "ls"]:
            if failure == "listing_failure":
                return SimpleNamespace(returncode=1, stdout="")
            name = calls[0][calls[0].index("--name") + 1]
            stopped = any(call[:2] == ["docker", "stop"] for call in calls)
            if stopped:
                stopped_queries += 1
            delayed = failure == "removal_delay" and stopped_queries == 1
            return SimpleNamespace(returncode=0, stdout=name if not stopped or failure == "retained_runtime" or delayed else "")
        elif command[:2] == ["docker", "stop"] and failure == "stop_failure":
            raise subprocess.CalledProcessError(1, command, stderr=PRIVATE_MARKER)
        elif command[:2] != ["docker", "stop"]:
            pytest.fail("Unexpected external process")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(drill.subprocess, "run", run)
    return calls


def test_managed_uses_private_files_loopback_and_removes_only_its_owned_container(drill, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RECOVERY_CONTAINER", "unrelated-preserved-container")
    calls = _managed_mock(drill, monkeypatch)
    with drill.disposable_database():
        source = Path(os.environ["DATABASE_URL_FILE"])
        environment = source.with_name("postgres.env")
        assert source.stat().st_mode & 0o777 == environment.stat().st_mode & 0o777 == 0o600
        url = make_url(source.read_text())
        assert url.host == "127.0.0.1" and url.port == 55445 and url.database == "knowledge_build_autonomy"
        command = calls[0]
        name = command[command.index("--name") + 1]
        assert os.environ["KNOWLEDGE_RECOVERY_CONTAINER"] == name
        assert command[command.index("--label") + 1] == "knowledge-layer.autonomy-drill=" + name
        assert command[command.index("-p") + 1] == "127.0.0.1:55445:5432"
        assert url.password and all(url.password not in argument for argument in command)
    assert calls[-2] == ["docker", "stop", "--time", "5", name]
    assert calls[-1][:3] == ["docker", "container", "ls"]
    assert not source.exists() and "DATABASE_URL_FILE" not in os.environ
    assert os.environ["KNOWLEDGE_RECOVERY_CONTAINER"] == "unrelated-preserved-container"


@pytest.mark.parametrize("failure", ["launch_timeout", "readiness_timeout", "body_failure"])
def test_managed_failure_still_inspects_and_removes_owned_runtime(drill, monkeypatch, failure):
    calls = _managed_mock(drill, monkeypatch, failure=failure)
    expected = RuntimeError if failure == "body_failure" else subprocess.TimeoutExpired
    with pytest.raises(expected), drill.disposable_database():
        raise RuntimeError("offline qualification failure")
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[-3][-1] == name and calls[-3][:2] == ["docker", "inspect"]
    assert calls[-2] == ["docker", "stop", "--time", "5", name]
    assert calls[-1][:3] == ["docker", "container", "ls"]
    assert "DATABASE_URL_FILE" not in os.environ


def test_managed_changed_ownership_refuses_to_stop_other_runtime(drill, monkeypatch):
    calls = _managed_mock(drill, monkeypatch, label_matches=False)
    with pytest.raises(RuntimeError, match="ownership changed"), drill.disposable_database():
        pass
    assert not any(command[:2] == ["docker", "stop"] for command in calls)
    assert "DATABASE_URL_FILE" not in os.environ


def test_managed_waits_for_delayed_docker_auto_removal(drill, monkeypatch):
    calls = _managed_mock(drill, monkeypatch, failure="removal_delay")
    with drill.disposable_database():
        pass
    assert sum(command[:3] == ["docker", "container", "ls"] for command in calls) == 3
    assert "DATABASE_URL_FILE" not in os.environ


@pytest.mark.parametrize("failure", ["listing_failure", "inspect_failure", "stop_failure", "retained_runtime"])
def test_managed_cleanup_uncertainty_fails_instead_of_claiming_runtime_removed(drill, monkeypatch, failure):
    calls = _managed_mock(drill, monkeypatch, failure=failure)
    expected = subprocess.CalledProcessError if failure == "stop_failure" else RuntimeError
    with pytest.raises(expected), drill.disposable_database():
        pass
    if failure in {"listing_failure", "inspect_failure"}:
        assert not any(command[:2] == ["docker", "stop"] for command in calls)
    assert "DATABASE_URL_FILE" not in os.environ


@pytest.mark.parametrize("failure", ["registry_cleanup", "table_drop", "operator_close"])
def test_fixture_teardown_attempts_role_drop_environment_removal_and_both_closes(
    drill, monkeypatch, tmp_path, failure,
):
    commands, stores = [], []

    class Engine:
        @contextmanager
        def begin(self):
            yield self

        def exec_driver_sql(self, statement, *_args):
            commands.append(statement)
            if failure == "table_drop" and statement.startswith("DROP TABLE"):
                raise RuntimeError("offline table drop failed")

    class Store:
        def __init__(self, **_kwargs):
            self.engine = Engine()
            self.closed = False
            stores.append(self)

        def create_connection(self, *_args, **_kwargs):
            return {"id": "owned-fixture-source"}

        def refresh_connection(self, *_args):
            raise RuntimeError("offline qualification stopped after source creation")

        def close(self):
            self.closed = True
            if failure == "operator_close" and self is stores[1]:
                raise RuntimeError("offline operator close failed")

    def cleanup(*_args):
        if failure == "registry_cleanup":
            raise RuntimeError("offline registry cleanup failed")
        return {}

    url = make_url("postgresql://fixture_owner@127.0.0.1:55445/knowledge_build_fixture")
    monkeypatch.setattr(drill, "_database_url", lambda: (str(url), url))
    monkeypatch.setattr(drill, "KnowledgeStore", Store)
    monkeypatch.setattr(drill, "AutonomyRegistry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(drill, "run", lambda *_args: {
        "revision": "0016_builds", "head": "0016_builds", "schema_matches": True})
    monkeypatch.setattr(drill, "ensure_current_schema", lambda *_args: None)
    monkeypatch.setattr(drill, "_census", lambda *_args: {})
    monkeypatch.setattr(drill, "_cleanup_registry", cleanup)
    report = {"checks": []}
    with pytest.raises(RuntimeError):
        drill.qualify(report, tmp_path / "report.json")
    assert any(command.startswith("DROP TABLE") for command in commands)
    assert any(command.startswith("DROP ROLE") for command in commands)
    assert len(stores) == 2 and all(store.closed for store in stores)
    assert not any(key.startswith("KNOWLEDGE_SOURCE_AUTONOMY_") for key in os.environ)
