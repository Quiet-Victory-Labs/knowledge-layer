"""Secret gate isolation and redaction, without real credentials or services."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_secrets.py"
spec = importlib.util.spec_from_file_location("secret_scanning_gate", SCRIPT)
assert spec and spec.loader
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)

CANARY = "private-detector-output-canary"


def _baseline():
    return {"version": "1.5.0", "plugins_used": [], "filters_used": [], "results": {
        "fixture.py": [
            {"filename": "fixture.py", "hashed_secret": "a" * 40, "type": "Fixture candidate", "line_number": 3},
            {"filename": "fixture.py", "hashed_secret": "b" * 40, "type": "Fixture candidate", "line_number": 7},
        ],
    }}


@pytest.fixture
def repository(tmp_path, monkeypatch):
    baseline = tmp_path / ".secrets.baseline"
    original = (json.dumps(_baseline(), indent=2) + "\n").encode()
    baseline.write_bytes(original)
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    monkeypatch.setattr(scanner.subprocess, "check_output", lambda *_args, **_kwargs: (
        b"fixture.py\0.secrets.baseline\0--option-name.py\0"
    ))
    return baseline, original


def _temporary_baseline(command, options, reviewed):
    assert command[:5] == ["uvx", "--from", "detect-secrets==1.5.0", "detect-secrets-hook", "--no-verify"]
    position = command.index("--baseline")
    path = Path(command[position + 1])
    assert command[position + 2:] == ["--", "fixture.py", "--option-name.py"]
    assert path != reviewed and path.read_bytes() == reviewed.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["timeout"] == 90 and options["check"] is False
    return path


@pytest.mark.parametrize("exit_code", [0, 3])
def test_clean_scan_uses_and_removes_a_private_copy_without_changing_reviewed_bytes(repository, monkeypatch, capsys, exit_code):
    reviewed, original = repository
    observed = []

    def run(command, **options):
        observed.append(_temporary_baseline(command, options, reviewed))
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == 0
    assert capsys.readouterr().out == "Secret candidate scan passed for 2 tracked files\n"
    assert reviewed.read_bytes() == original
    assert observed and not observed[0].exists() and not observed[0].parent.exists()


@pytest.mark.parametrize("change", ["line-move", "disappearance", "all-disappear"])
def test_exit_three_allows_only_known_candidate_line_moves_and_disappearance(repository, monkeypatch, change):
    reviewed, original = repository
    observed = []

    def run(command, **options):
        path = _temporary_baseline(command, options, reviewed)
        observed.append(path)
        baseline = json.loads(path.read_bytes())
        if change == "line-move":
            baseline["results"]["fixture.py"][0]["line_number"] = 999
        elif change == "disappearance":
            baseline["results"]["fixture.py"].pop()
        else:
            baseline["results"] = {}
        path.write_text(json.dumps(baseline))
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == 0
    assert reviewed.read_bytes() == original
    assert not observed[0].parent.exists()


@pytest.mark.parametrize("change", ["hash", "filename", "type", "new-candidate"])
def test_exit_three_never_allows_an_added_candidate_identity(repository, monkeypatch, capsys, change):
    reviewed, original = repository
    observed = []

    def run(command, **options):
        path = _temporary_baseline(command, options, reviewed)
        observed.append(path)
        baseline = json.loads(path.read_bytes())
        candidate = baseline["results"]["fixture.py"][0]
        if change == "hash":
            candidate["hashed_secret"] = "c" * 40
        elif change == "filename":
            baseline["results"]["another.py"] = baseline["results"].pop("fixture.py")
            for candidate in baseline["results"]["another.py"]:
                candidate["filename"] = "another.py"
        elif change == "type":
            candidate["type"] = "Another candidate type"
        else:
            added = deepcopy(candidate)
            added["hashed_secret"] = "c" * 40
            baseline["results"]["fixture.py"].append(added)
        path.write_text(json.dumps(baseline))
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == 1
    assert capsys.readouterr().out == scanner.FAILURE_MESSAGE + "\n"
    assert reviewed.read_bytes() == original
    assert not observed[0].parent.exists()


@pytest.mark.parametrize("invalid", ["json", "results", "version", "missing-type", "bad-hash", "filename-mismatch", "duplicate-key"])
def test_exit_three_refuses_an_invalid_temporary_baseline(repository, monkeypatch, capsys, invalid):
    reviewed, original = repository
    observed = []

    def run(command, **options):
        path = _temporary_baseline(command, options, reviewed)
        observed.append(path)
        baseline = json.loads(path.read_bytes())
        if invalid == "json":
            raw = CANARY.encode()
        elif invalid == "duplicate-key":
            raw = b'{"version":"1.5.0","results":{},"results":{}}'
        else:
            if invalid == "results":
                baseline["results"] = []
            elif invalid == "version":
                baseline["version"] = "unexpected"
            elif invalid == "missing-type":
                baseline["results"]["fixture.py"][0].pop("type")
            elif invalid == "bad-hash":
                baseline["results"]["fixture.py"][0]["hashed_secret"] = CANARY
            else:
                baseline["results"]["fixture.py"][0]["filename"] = CANARY
            raw = json.dumps(baseline).encode()
        path.write_bytes(raw)
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == 1
    output = capsys.readouterr()
    assert output.out == scanner.FAILURE_MESSAGE + "\n" and output.err == ""
    assert CANARY not in output.out
    assert reviewed.read_bytes() == original
    assert not observed[0].parent.exists()


@pytest.mark.parametrize("exit_code", [1, 2, -9])
def test_detector_refusal_and_other_exits_never_pass(repository, monkeypatch, capsys, exit_code):
    reviewed, original = repository
    observed = []

    def run(command, **options):
        path = _temporary_baseline(command, options, reviewed)
        observed.append(path)
        path.write_text(json.dumps(_baseline() | {"results": {}}))
        return SimpleNamespace(returncode=exit_code, stdout=CANARY, stderr=CANARY)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == 1
    output = capsys.readouterr()
    assert output.out == scanner.FAILURE_MESSAGE + "\n" and output.err == ""
    assert reviewed.read_bytes() == original
    assert not observed[0].parent.exists()


@pytest.mark.parametrize("failure", ["timeout", "launch"])
def test_timeout_and_launch_failure_redact_diagnostics_and_remove_copy(repository, monkeypatch, capsys, failure):
    reviewed, original = repository
    observed = []

    def run(command, **options):
        observed.append(_temporary_baseline(command, options, reviewed))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 90, output=CANARY.encode(), stderr=CANARY.encode())
        raise OSError(CANARY)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == 1
    output = capsys.readouterr()
    assert output.out == scanner.FAILURE_MESSAGE + "\n" and output.err == ""
    assert CANARY not in output.out + output.err
    assert reviewed.read_bytes() == original
    assert not observed[0].parent.exists()


def test_invalid_reviewed_baseline_refuses_before_detector(repository, monkeypatch, capsys):
    reviewed, _original = repository
    reviewed.write_text(CANARY)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid reviewed baseline must refuse before invoking detector")

    monkeypatch.setattr(scanner.subprocess, "run", unexpected)
    assert scanner.main() == 1
    assert capsys.readouterr().out == scanner.FAILURE_MESSAGE + "\n"
    assert reviewed.read_text() == CANARY


def test_actual_subprocess_output_is_discarded(repository, monkeypatch, tmp_path, capfd):
    reviewed, original = repository
    executable = tmp_path / "uvx"
    executable.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"data = {CANARY!r}.encode() * 10000\n"
        "os.write(1, data)\nos.write(2, data)\nsys.exit(1)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert scanner.main() == 1
    output = capfd.readouterr()
    assert output.out == scanner.FAILURE_MESSAGE + "\n" and output.err == ""
    assert CANARY not in output.out + output.err
    assert reviewed.read_bytes() == original


def test_baseline_read_budget_refuses_before_detector(repository, monkeypatch):
    reviewed, original = repository
    monkeypatch.setattr(scanner, "MAX_BASELINE_BYTES", len(original) - 1)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Oversized baseline must refuse before invoking detector")

    monkeypatch.setattr(scanner.subprocess, "run", unexpected)
    assert scanner.main() == 1
    assert reviewed.read_bytes() == original
