"""Exercise real subprocess boundaries, source scoping and output failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from knowledge_layer import connector_isolation as isolation
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.store import KnowledgeStore
from knowledge_layer.worker import worker_scope


def test_environment_contains_only_selected_profile_and_no_authority(tmp_path, monkeypatch):
    secret = tmp_path / "selected.json"
    secret.write_text("selected-credential")
    secret.chmod(0o600)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_SELECTED_FILE", str(secret))
    for name in ("DATABASE_URL", "KNOWLEDGE_OIDC_CLIENT_SECRET", "KNOWLEDGE_SOURCE_OTHER",
                 "KNOWLEDGE_SOURCE_OTHER_FILE", "AWS_SECRET_ACCESS_KEY", "HTTP_PROXY",
                 "OPENAI_API_KEY", "PYTHONPATH", "KNOWLEDGE_AGENT_TOKEN"):
        monkeypatch.setenv(name, "must-not-inherit")
    environment = isolation.source_environment("postgres", {"secret_ref": "KNOWLEDGE_SOURCE_SELECTED"})
    assert environment["KNOWLEDGE_SOURCE_SELECTED"] == "selected-credential"
    assert not any("must-not-inherit" == value for value in environment.values())
    assert not any(name.endswith("_FILE") for name in environment)
    process = subprocess.run([sys.executable, "-I", "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
                             env=environment, capture_output=True, check=True, timeout=5)
    actual = json.loads(process.stdout)
    assert actual["KNOWLEDGE_SOURCE_SELECTED"] == "selected-credential"
    assert not any("must-not-inherit" == value for value in actual.values())


def test_real_isolated_local_curation_preserves_ordinary_discovery(tmp_path, monkeypatch):
    (tmp_path / "records.csv").write_text("id,amount\na,12\nb,0\n")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-should-not-enter-connector")
    result = isolation.isolated_curator("local_files", {"root": str(tmp_path), "path": ".",
        "include": "*.csv", "max_files": 2, "max_rows": 100, "source_id": "isolation-test"})
    assert result["status"] != "error"
    assert result["assets"] and result["assets"][0]["schema"]
    assert "secret-should-not-enter-connector" not in json.dumps(result)


def test_catalog_map_is_narrowed_to_one_profile(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_ICEBERG_PROFILES", json.dumps({"one": "KNOWLEDGE_SOURCE_ONE", "two": "KNOWLEDGE_SOURCE_TWO"}))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_ONE", '{"uri":"https://one.example"}')
    monkeypatch.setenv("KNOWLEDGE_SOURCE_TWO", "other-secret")
    env = isolation.source_environment("iceberg", {"catalog": "one"})
    assert json.loads(env["KNOWLEDGE_ICEBERG_PROFILES"]) == {"one": "KNOWLEDGE_SOURCE_ONE"}
    assert "KNOWLEDGE_SOURCE_TWO" not in env


@pytest.mark.parametrize("profiles", [
    {"one": "KNOWLEDGE_SOURCE_ONE", "two": "DATABASE_URL"},
    {"one": "KNOWLEDGE_SOURCE_ONE", "bad/alias": "KNOWLEDGE_SOURCE_TWO"},
    {"one": "KNOWLEDGE_SOURCE_ONE", **{f"alias{i}": "KNOWLEDGE_SOURCE_TWO" for i in range(100)}},
])
def test_invalid_catalog_map_cannot_be_laundered_by_narrowing(profiles, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_ICEBERG_PROFILES", json.dumps(profiles))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_ONE", '{"uri":"https://one.example"}')
    with pytest.raises(ConnectorError, match="map is invalid"):
        isolation.source_environment("iceberg", {"catalog": "one"})


def test_coordinator_keeps_rotating_files_but_not_other_source_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_text("original")
    source.chmod(0o600)
    policy = tmp_path / "policy"
    policy.write_text('{"version":1,"sources":[]}')
    policy.chmod(0o600)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_SELECTED_FILE", str(source))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_OTHER_FILE", "/unrelated/secret")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_POLICY_FILE", str(policy))
    env = isolation.coordinator_environment("postgres", {"secret_ref": "KNOWLEDGE_SOURCE_SELECTED"})
    assert env["KNOWLEDGE_SOURCE_SELECTED_FILE"] == str(source)
    assert env["KNOWLEDGE_SOURCE_POLICY_FILE"] == str(policy)
    assert "KNOWLEDGE_SOURCE_SELECTED" not in env and "KNOWLEDGE_SOURCE_OTHER_FILE" not in env


@pytest.mark.parametrize("failure", ["timeout", "excess", "invalid"])
def test_real_connector_failures_are_bounded_and_reaped(monkeypatch, failure):
    real_popen = subprocess.Popen
    children = []
    code = "import time; time.sleep(5)" if failure == "timeout" else "print('x'*4096)" if failure == "excess" else "print('invalid output')"

    def child(_command, **kwargs):
        process = real_popen([sys.executable, "-I", "-c", code], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", child)
    monkeypatch.setattr(isolation, "MAX_WIRE_BYTES", 2048)
    with pytest.raises(ConnectorError):
        isolation.run_connector("snapshot", "local_files", {}, timeout_seconds=0.2)
    assert children and children[0].poll() is not None


def test_dedicated_worker_never_claims_other_source_or_tenant(tmp_path):
    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'queue.db'}")
    try:
        first = store.create_connection("demo", kind="local_files", name="Other source", config={})
        selected = store.create_connection("demo", kind="local_files", name="Selected source", config={})
        foreign = store.create_connection("other", kind="local_files", name="Other tenant", config={})
        queue = RefreshQueue(store)
        for tenant, source in (("demo", first), ("other", foreign), ("demo", selected)):
            queue.enqueue(tenant, source["id"])
        job = queue.claim("selected-worker", source_scope=("demo", (selected["id"],)))
        assert job["connection_id"] == selected["id"] and job["tenant_id"] == "demo"
        assert queue.claim("selected-worker", source_scope=("demo", (selected["id"],))) is None
    finally:
        store.close()


@pytest.mark.parametrize("identifiers,tenant", [("", "demo"), ("conn_a", ""), ("conn_a,conn_a", "demo"), ("conn_a,invalid", "demo")])
def test_invalid_dedicated_worker_scope_refuses(identifiers, tenant, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_WORKER_CONNECTION_IDS", identifiers)
    monkeypatch.setenv("KNOWLEDGE_WORKER_TENANT", tenant)
    with pytest.raises(ValueError):
        worker_scope()


@pytest.mark.parametrize("image,network", [("repo:latest", "none"), ("sha256:" + "a" * 64, "host"),
                                          ("sha256:" + "a" * 64, "container:other")])
def test_container_runtime_refuses_mutable_images_and_host_or_shared_namespaces(image, network, monkeypatch):
    monkeypatch.setattr(isolation.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("KNOWLEDGE_CONNECTOR_IMAGE", image)
    monkeypatch.setenv("KNOWLEDGE_CONNECTOR_NETWORK", network)
    with pytest.raises(ConnectorError):
        isolation.container_command("knowledge-connector-fixture")


def test_container_packet_cannot_introduce_registry_credentials():
    packet = {"operation": "snapshot", "kind": "local_files", "config": {}, "request": None,
              "environment": {"DATABASE_URL": "must-not-be-accepted"}}
    result = subprocess.run([sys.executable, "-I", "-m", "knowledge_layer.connector_process",
                             "--environment-from-packet"], input=json.dumps(packet).encode(),
                            capture_output=True, timeout=5, check=False, env={"PATH": os.environ.get("PATH", "")})
    assert result.returncode == 2 and not result.stdout and not result.stderr


@pytest.mark.parametrize("suffix", ["ALPHA", "1VALID", "_VALID"])
def test_container_protocol_preserves_all_valid_source_reference_forms(tmp_path, suffix):
    (tmp_path / "control.csv").write_text("id,value\n1,12\n")
    packet = {"operation": "snapshot", "kind": "local_files",
        "config": {"root": str(tmp_path), "path": ".", "source_id": "protocol-control"}, "request": None,
        "environment": {"KNOWLEDGE_SOURCE_" + suffix: "fixture-only"}}
    result = subprocess.run([sys.executable, "-I", "-m", "knowledge_layer.connector_process",
        "--environment-from-packet"], input=json.dumps(packet).encode(), capture_output=True,
        timeout=5, check=False, env={"PATH": os.environ.get("PATH", "")})
    assert result.returncode == 0 and json.loads(result.stdout)["assets"]


def test_scoped_dispatch_cannot_pause_or_enqueue_another_source_or_tenant(tmp_path):
    from datetime import UTC, datetime, timedelta

    from knowledge_layer.schedules import RefreshSchedules

    store = KnowledgeStore(database_url=f"sqlite:///{tmp_path / 'dispatch.db'}")
    try:
        selected = store.create_connection("demo", kind="local_files", name="Selected", config={})
        foreign = store.create_connection("other", kind="postgres", name="Foreign", config={
            "secret_ref": "KNOWLEDGE_SOURCE_FOREIGN", "schema": "public"})
        same_tenant = store.create_connection("demo", kind="postgres", name="Other selected", config={
            "secret_ref": "KNOWLEDGE_SOURCE_OTHER", "schema": "public"})
        now = datetime(2026, 10, 3, 12, tzinfo=UTC)
        schedules = RefreshSchedules(store)
        for tenant, source in (("demo", selected), ("other", foreign), ("demo", same_tenant)):
            schedules.set(tenant, source["id"], {"cadence": "hourly", "timezone": "UTC", "minute": 0}, now=now)
        result = schedules.dispatch_due({"local_files"}, now=now + timedelta(hours=1),
                                        source_scope=("demo", (selected["id"],)))
        assert len(result) == 1 and result[0]["status"] == "enqueued"
        for tenant, source in (("other", foreign), ("demo", same_tenant)):
            untouched = schedules.get(tenant, source["id"])
            assert untouched["paused"] is False and untouched["last_error"] is None
            assert untouched["last_job_id"] is None
    finally:
        store.close()
