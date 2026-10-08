"""A stopped Docker exec transport must not hold restore verification forever."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup_registry


def test_fingerprint_host_deadline_kills_stalled_transport(monkeypatch):
    monkeypatch.setattr(backup_registry, "command", lambda *_args: b"alembic_version\n")
    monkeypatch.setattr(backup_registry, "compose", lambda *_args: [sys.executable, "-c", "import time; time.sleep(60)"])
    clock = iter([0.0, 601.0])
    monkeypatch.setattr(backup_registry, "monotonic", lambda: next(clock))
    processes = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(backup_registry.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="registry_fingerprint_timeout"):
        backup_registry.fingerprint(Path("/unused"), "knowledge_layer")
    assert len(processes) == 1 and processes[0].poll() is not None


def test_fingerprint_streams_sorted_row_hashes_without_record_payloads(monkeypatch):
    rows = ("a" * 64 + "\n" + "b" * 64 + "\n").encode()
    monkeypatch.setattr(backup_registry, "command", lambda *_args: b"alembic_version\n")
    monkeypatch.setattr(backup_registry, "compose", lambda *_args: [sys.executable, "-c", "print('a'*64); print('b'*64)"])
    assert backup_registry.fingerprint(Path("/unused"), "knowledge_layer") == {
        "alembic_version": {"rows": 2, "sha256": hashlib.sha256(rows).hexdigest()},
    }
