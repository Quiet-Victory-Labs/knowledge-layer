"""Offline recovery effects and adversarial archive/authority checks."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile

import pytest

from scripts import self_host
from scripts import self_host_recovery as recovery

APP = "sha256:" + "a" * 64
PG = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64


def private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    source = tmp_path / "original"
    config = self_host.initialize(source)
    private(source / "postgres/18/docker/PG_VERSION", "18\n")
    private(source / "postgres/18/docker/global/pg_control", b"synthetic-physical-control")
    private(source / "postgres/18/docker/base/accepted-pins", b"snapshot=accepted;digest=publication-pin")
    private(source / "sources/customer/catalog.md", "Synthetic source documentation")
    owner = {"version": 1, "name": "owner", "salt": "1" * 64, "hash": "2" * 64,
             "session_version": "3" * 64}
    private(source / "state/auth/local-auth.json", json.dumps(owner))
    private(source / self_host.RUNTIME_IMAGES, json.dumps({
        "version": 1, "project": config["project"], "images": {"app": APP, "postgres": PG}}))
    calls = []
    def docker(args, **kwargs):
        calls.append(args)
        if args[:2] == ["info", "--format"]:
            output = "linux/aarch64"
        elif args[:2] == ["image", "inspect"]:
            image = args[-1]
            assert image in {APP, PG}, "recovery must inspect recorded IDs, not mutable tags"
            output = json.dumps({"Id": image, "Os": "linux", "Architecture": "arm64", "Variant": "v8",
                                 "RepoDigests": ["registry.example/knowledge@" + image],
                                 "Config": {"Env": ["PG_MAJOR=18"]}})
        else:
            pytest.fail("Recovery may not start, stop, pull or tag containers")
        return subprocess.CompletedProcess(args, 0, output, "")
    monkeypatch.setattr(self_host, "docker", docker)
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: set())
    monkeypatch.setattr(recovery, "_checkout", lambda *a: {"commit": "d" * 40, "dirty": True})
    return source, config, tmp_path / "bundle.tar", owner, calls


def capture(installation):
    source, config, bundle, _, _ = installation
    return self_host.backup_installation(source, config, bundle)


def rewrite_bundle(bundle, change):
    """Re-sign local integrity after tampering to test structural gates too."""
    receipt = json.loads(bundle.with_name(bundle.name + ".json").read_bytes())
    with tarfile.open(bundle, "r:") as archive:
        members = [(item, archive.extractfile(item).read() if item.isfile() else None) for item in archive]
    members = change(members)
    with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as archive:
        for member, contents in members:
            archive.addfile(member, io.BytesIO(contents) if contents is not None else None)
    contents = bundle.read_bytes()
    receipt.update(bytes=len(contents), sha256=hashlib.sha256(contents).hexdigest())
    private(bundle.with_name(bundle.name + ".json"), json.dumps(receipt))


def test_complete_cold_roundtrip_keeps_owner_secrets_sources_database_and_pins(installation):
    source, config, bundle, owner, calls = installation
    before = {str(item.relative_to(source)): item.read_bytes() for item in source.rglob("*") if item.is_file()}
    result = capture(installation)
    assert result["receipt"]["images"]["app"]["image_id"] == APP
    assert result["receipt"]["checkout"] == {"commit": "d" * 40, "dirty": True}
    restored = source.parent / "restored"
    report = self_host.restore_installation(bundle, restored, port=4273)
    saved = json.loads((restored / self_host.MANIFEST).read_bytes())
    assert saved["project"] != config["project"] and saved["checkout"] == str(self_host.ROOT)
    assert saved["port"] == 4273 and saved["public_url"] == "http://127.0.0.1:4273"
    assert saved["recovery_image_id"] == APP
    services = json.loads((restored / "compose.yaml").read_bytes())["services"]
    assert services["postgres"]["image"] == PG
    assert "build" not in services["postgres"] and services["postgres"]["pull_policy"] == "never"
    changed = {self_host.MANIFEST, "compose.yaml", "state/auth/local-auth.json", self_host.RUNTIME_IMAGES}
    for relative, contents in before.items():
        if relative not in changed:
            assert (restored / relative).read_bytes() == contents
        assert (source / relative).read_bytes() == contents
    restored_owner = json.loads((restored / "state/auth/local-auth.json").read_bytes())
    assert restored_owner | {"session_version": owner["session_version"]} == owner
    assert restored_owner["session_version"] != owner["session_version"]
    assert report["browser_sessions_invalidated"] is True
    assert report["filesystem_verified"] is True and report["semantic_pins_verified"] is False
    assert report["database_started"] is False
    assert not (restored / self_host.RUNTIME_IMAGES).exists()
    for item in [restored, *restored.rglob("*")]:
        assert stat.S_IMODE(item.stat().st_mode) == (0o700 if item.is_dir() else 0o600)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600 and calls


@pytest.mark.parametrize("condition", ["running", "pid", "wrong_major", "missing_receipt"])
def test_backup_refuses_without_complete_stopped_pg_and_runtime_identity(installation, monkeypatch, condition):
    source, _, bundle, _, _ = installation
    if condition == "running":
        monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: {"a" * 64})
    elif condition == "pid":
        private(source / "postgres/18/docker/postmaster.pid", "42")
    elif condition == "wrong_major":
        private(source / "postgres/18/docker/PG_VERSION", "17")
    else:
        (source / self_host.RUNTIME_IMAGES).unlink()
    with pytest.raises(ValueError):
        capture(installation)
    assert not bundle.exists() and (source / self_host.MANIFEST).exists()


def test_backup_uses_actual_runtime_ids_when_compose_tag_has_moved(installation):
    source, _, _, _, calls = installation
    compose = json.loads((source / "compose.yaml").read_bytes())
    compose["services"]["api"]["image"] = "arbitrary:new-tag"
    private(source / "compose.yaml", json.dumps(compose))
    capture(installation)
    inspected = [args[-1] for args in calls if args[:2] == ["image", "inspect"]]
    assert inspected == [APP, PG]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_backup_refuses_paths_outside_regular_owned_scope(installation, kind):
    source, _, bundle, _, _ = installation
    path = source / "sources/bad"
    if kind == "symlink":
        path.symlink_to(source / "secrets/agent_token")
    elif kind == "hardlink":
        os.link(source / "secrets/agent_token", path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises(ValueError):
        capture(installation)
    assert not bundle.exists()


def test_backup_finite_byte_and_item_limits(installation, monkeypatch):
    _, _, bundle, _, _ = installation
    monkeypatch.setattr(recovery, "MAX_ITEMS", 1)
    with pytest.raises(ValueError, match="item_limit"):
        capture(installation)
    assert not bundle.exists()
    monkeypatch.setattr(recovery, "MAX_ITEMS", 10000)
    monkeypatch.setattr(recovery, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="byte_limit"):
        capture(installation)


def test_restore_refuses_corrupted_bundle_before_creating_destination(installation):
    source, _, bundle, _, _ = installation
    capture(installation)
    with bundle.open("r+b") as stream:
        stream.seek(1200)
        stream.write(b"corrupt")
    restored = source.parent / "restored"
    with pytest.raises(ValueError, match="integrity_mismatch"):
        self_host.restore_installation(bundle, restored)
    assert not restored.exists() and (source / self_host.MANIFEST).exists()


@pytest.mark.parametrize("kind", ["duplicate_control", "duplicate_file", "extra", "traversal", "symlink", "missing"])
def test_restore_rejects_structural_tampering_even_with_matching_checksum(installation, kind):
    source, _, bundle, _, _ = installation
    capture(installation)
    def change(members):
        if kind == "duplicate_control":
            return members + [members[0]]
        if kind == "duplicate_file":
            return members + [next(item for item in members if item[0].name == "secrets/agent_token")]
        if kind == "missing":
            return [item for item in members if item[0].name != "secrets/agent_token"]
        member = tarfile.TarInfo("../escape" if kind == "traversal" else "extra")
        if kind == "symlink":
            member.name, member.type, member.linkname = "secrets/agent_token", tarfile.SYMTYPE, "/tmp/outside"
            members = [item for item in members if item[0].name != member.name]
        return members + [(member, b"" if member.isfile() else None)]
    rewrite_bundle(bundle, change)
    restored = source.parent / "restored"
    with pytest.raises(ValueError):
        self_host.restore_installation(bundle, restored)
    assert not restored.exists() and bundle.exists() and (source / self_host.MANIFEST).exists()
    for partial in source.parent.glob(".knowledge-restore-*"):
        assert stat.S_IMODE(partial.stat().st_mode) == 0o700
    assert not (source.parent / "escape").exists()


def test_restore_refuses_existing_target_and_shared_parent(installation):
    source, _, bundle, _, _ = installation
    capture(installation)
    restored = source.parent / "restored"
    restored.mkdir(mode=0o700)
    sentinel = private(restored / "keep", "original data")
    with pytest.raises(ValueError, match="new_restore"):
        self_host.restore_installation(bundle, restored)
    assert sentinel.read_text() == "original data"
    shared = source.parent / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    with pytest.raises(ValueError, match="private_recovery_parent"):
        self_host.restore_installation(bundle, shared / "new")
    assert list(shared.iterdir()) == []


def test_restore_refuses_wrong_host_platform_and_pg_image_major(installation, monkeypatch):
    source, _, bundle, _, _ = installation
    capture(installation)
    original = self_host.docker
    def wrong_host(args, **kwargs):
        if args[0] == "info":
            return subprocess.CompletedProcess(args, 0, "linux/amd64", "")
        return original(args, **kwargs)
    monkeypatch.setattr(self_host, "docker", wrong_host)
    with pytest.raises(ValueError, match="platform_mismatch"):
        self_host.restore_installation(bundle, source.parent / "restored")
    def wrong_major(args, **kwargs):
        result = original(args, **kwargs)
        if args[:2] == ["image", "inspect"] and args[-1] == PG:
            payload = json.loads(result.stdout)
            payload["Config"]["Env"] = ["PG_MAJOR=17"]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        return result
    monkeypatch.setattr(self_host, "docker", wrong_major)
    with pytest.raises(ValueError, match="major_image_mismatch"):
        self_host.restore_installation(bundle, source.parent / "restored")


def test_restore_failure_retains_private_partial_bundle_original_and_no_commit(installation, monkeypatch):
    source, _, bundle, _, _ = installation
    capture(installation)
    original = self_host.write_private
    def fail(path, *args):
        if path.name == "recovery.json":
            raise OSError("synthetic disk failure")
        return original(path, *args)
    monkeypatch.setattr(self_host, "write_private", fail)
    restored = source.parent / "restored"
    with pytest.raises(OSError):
        self_host.restore_installation(bundle, restored)
    assert not restored.exists() and bundle.exists() and (source / self_host.MANIFEST).exists()
    partials = list(source.parent.glob(".knowledge-restore-*"))
    assert len(partials) == 1 and stat.S_IMODE(partials[0].stat().st_mode) == 0o700
    assert (partials[0] / "secrets/agent_token").exists()
    assert not (partials[0] / self_host.MANIFEST).exists()


def test_failed_backup_keeps_private_partial_without_success_sidecar(installation, monkeypatch):
    _, _, bundle, _, _ = installation
    original = recovery._scan
    count = 0
    def changing(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("backup_files_changed")
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "_scan", changing)
    with pytest.raises(ValueError):
        capture(installation)
    assert bundle.exists() and stat.S_IMODE(bundle.stat().st_mode) == 0o600
    assert not bundle.with_name(bundle.name + ".json").exists()


def test_cli_safe_backup_and_restore_reports_never_print_secrets(installation, capsys):
    source, _, bundle, _, _ = installation
    assert self_host.main(["--install-dir", str(source), "backup", "--output", str(bundle)]) == 0
    backup_report = capsys.readouterr().out
    restored = source.parent / "restored"
    assert self_host.main(["--install-dir", str(restored), "restore", "--backup", str(bundle), "--port", "4273"]) == 0
    restore_report = capsys.readouterr().out
    assert json.loads(restore_report)["images"]["app"]["image_id"] == APP
    for item in (source / "secrets").iterdir():
        assert item.read_text() not in backup_report + restore_report
    assert '"files"' not in backup_report


def test_runtime_receipt_binds_container_ids_services_project_and_actual_images(tmp_path, monkeypatch):
    source = tmp_path / "original"
    config = self_host.initialize(source)
    identifiers = {"1" * 64, "2" * 64, "3" * 64, "4" * 64}
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: identifiers)
    rows = [{"id": identifier, "image_id": PG if service == "postgres" else APP,
             "project": config["project"], "service": service}
            for identifier, service in zip(sorted(identifiers), ["api", "postgres", "worker", "migrate"], strict=True)]
    def inspect(args, **kwargs):
        assert args[:2] == ["container", "inspect"] and set(args[4:]) == identifiers
        assert "{{json .Image}}" in args[3] and "{{json .Config}}" not in args[3]
        return subprocess.CompletedProcess(args, 0, "\n".join(json.dumps(row) for row in rows), "")
    monkeypatch.setattr(self_host, "docker", inspect)
    result = self_host.record_runtime_images(source, config)
    assert result["images"] == {"app": APP, "postgres": PG}
    receipt = (source / self_host.RUNTIME_IMAGES).read_bytes()
    rows[-1]["image_id"] = OTHER
    with pytest.raises(ValueError, match="images_differ"):
        self_host.record_runtime_images(source, config)
    assert (source / self_host.RUNTIME_IMAGES).read_bytes() == receipt
    rows[-1]["image_id"], rows[0]["project"] = APP, "knowledge-layer-" + "0" * 12
    with pytest.raises(ValueError, match="inventory_invalid"):
        self_host.record_runtime_images(source, config)


def test_restored_start_refuses_implicit_upgrade_and_moved_app_tag_before_compose(tmp_path, monkeypatch):
    source = tmp_path / "original"
    config = self_host.initialize(source)
    config["recovery_image_id"] = APP
    monkeypatch.setattr(self_host, "compose", lambda *a, **k: pytest.fail("No service may start"))
    with pytest.raises(self_host.OperationError, match="requires start --no-build"):
        self_host.start(source, config)
    monkeypatch.setattr(self_host, "docker", lambda *a, **k: subprocess.CompletedProcess([], 0, OTHER, ""))
    with pytest.raises(self_host.OperationError, match="does not match"):
        self_host.start(source, config, no_build=True)


def test_explicit_restored_upgrade_records_new_guard_and_preserves_credentials(tmp_path, monkeypatch):
    source = tmp_path / "original"
    config = self_host.initialize(source)
    config["recovery_image_id"] = APP
    before = (source / "secrets/agent_token").read_bytes()
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: set())
    monkeypatch.setattr(self_host, "require_free_port", lambda *a: None)
    monkeypatch.setattr(self_host, "record_runtime_images", lambda *a: {"images": {"app": OTHER, "postgres": PG}})
    commands = []
    monkeypatch.setattr(self_host, "compose", lambda p, c, a, **k: commands.append(a))
    self_host.start(source, config, allow_upgrade=True)
    assert commands == [["build", "api"], ["up", "--detach", "--wait", "--wait-timeout", "180"]]
    assert json.loads((source / self_host.MANIFEST).read_bytes())["recovery_image_id"] == OTHER
    assert (source / "secrets/agent_token").read_bytes() == before


def test_bounded_tar_header_reads_refuse_unbounded_allocations():
    wrapper = recovery._BoundedFile(io.BytesIO(b"x"), deadline=float("inf"))
    with pytest.raises(ValueError, match="read_budget"):
        wrapper.read(recovery.MAX_METADATA + 1)
    with pytest.raises(ValueError, match="read_budget"):
        wrapper.read()


@pytest.mark.parametrize("member_type", [tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME,
                                          tarfile.GNUTYPE_LONGLINK, tarfile.GNUTYPE_SPARSE])
def test_hostile_extended_header_is_rejected_before_unbounded_read(installation, monkeypatch, member_type):
    source, _, bundle, _, _ = installation
    capture(installation)
    original = bundle.read_bytes()
    with tarfile.open(bundle, "r:") as archive:
        control = archive.next()
        boundary = 512 + (control.size + 511) // 512 * 512
    extended = tarfile.TarInfo("hostile-extended-header")
    extended.type, extended.size = member_type, recovery.MAX_METADATA + 512
    contents = original[:boundary] + extended.tobuf(tarfile.USTAR_FORMAT) + original[boundary:]
    private(bundle, contents)
    sidecar = bundle.with_name(bundle.name + ".json")
    receipt = json.loads(sidecar.read_bytes())
    receipt.update(bytes=len(contents), sha256=hashlib.sha256(contents).hexdigest())
    private(sidecar, json.dumps(receipt))
    def refuse_tar_parser(*_args, **_kwargs):
        pytest.fail("Hostile extension reached the automatic tar parser")
    monkeypatch.setattr(recovery.tarfile, "open", refuse_tar_parser)
    with pytest.raises(ValueError, match="archive_scope_or_type_refused"):
        self_host.restore_installation(bundle, source.parent / "restored")
    assert not (source.parent / "restored").exists()


def test_restored_installation_can_be_backed_up_again_after_runtime_record(installation):
    source, _, bundle, _, _ = installation
    capture(installation)
    first = source.parent / "first"
    self_host.restore_installation(bundle, first)
    config = json.loads((first / self_host.MANIFEST).read_bytes())
    private(first / self_host.RUNTIME_IMAGES, json.dumps({
        "version": 1, "project": config["project"], "images": {"app": APP, "postgres": PG}}))
    second_bundle = source.parent / "second.tar"
    self_host.backup_installation(first, config, second_bundle)
    second = source.parent / "second"
    report = self_host.restore_installation(second_bundle, second)
    assert report["project"] != config["project"]
    assert json.loads((second / "recovery.json").read_bytes()) == report
    assert (first / "secrets/agent_token").read_bytes() == (second / "secrets/agent_token").read_bytes()


def test_missing_runtime_identity_still_stops_and_invalidates_stale_receipt(tmp_path, monkeypatch):
    source = tmp_path / "original"
    config = self_host.initialize(source)
    private(source / self_host.RUNTIME_IMAGES, "old receipt")
    monkeypatch.setattr(self_host, "project_container_ids", lambda *a, **k: {"a" * 64})
    def invalid(*args):
        raise ValueError("raw private diagnostic")
    monkeypatch.setattr(self_host, "record_runtime_images", invalid)
    commands = []
    monkeypatch.setattr(self_host, "compose", lambda p, c, a, **k: commands.append(a))
    with pytest.raises(self_host.OperationError, match="verified start and stop"):
        self_host.stop(source, config)
    assert commands == [["down"]] and not (source / self_host.RUNTIME_IMAGES).exists()
    assert (source / "secrets/database_url").exists()


@pytest.mark.parametrize("public_url", ["http://[::1]:4173", "http://127.0.0.1:4444"])
def test_restore_refuses_unreachable_or_mismatched_loopback_override(installation, public_url):
    source, _, bundle, _, _ = installation
    capture(installation)
    restored = source.parent / "restored"
    with pytest.raises(ValueError):
        self_host.restore_installation(bundle, restored, public_url=public_url)
    assert not restored.exists() and bundle.exists() and (source / self_host.MANIFEST).exists()
    assert all(not (item / self_host.MANIFEST).exists() for item in source.parent.glob(".knowledge-restore-*"))


def test_restore_rechecks_retained_manifest_loopback_binding(installation):
    source, config, bundle, _, _ = installation
    config["public_url"] = "http://[::1]:4173"
    private(source / self_host.MANIFEST, json.dumps(config))
    capture(installation)
    with pytest.raises(ValueError, match="ipv4_loopback"):
        self_host.restore_installation(bundle, source.parent / "restored")
    assert not (source.parent / "restored").exists()
