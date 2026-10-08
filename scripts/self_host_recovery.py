"""Bounded, private cold recovery for the portable Compose installation.

This copies the PostgreSQL cluster, not a portable logical SQL dump. It requires
stopped services and the exact locally available PostgreSQL image/platform. It
never starts a service, replaces an installation, or removes partial evidence.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SCHEMA = "knowledge-self-host-backup-v1"
CONTROL = "_knowledge_backup.json"
PENDING = "_knowledge_restore_manifest.json"
MAX_ITEMS = 10_000
MAX_BYTES = 2 * 1024**3
MAX_METADATA = 8 * 1024**2
MAX_ARCHIVE = MAX_BYTES + MAX_ITEMS * 2048 + MAX_METADATA
TIME_LIMIT = 600


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_backup_fields")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique)


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode()) > 255
            or any(char in value for char in "\\\0\r\n") or value in {CONTROL, PENDING}):
        raise ValueError("backup_path_refused")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")) or str(path) != value:
        raise ValueError("backup_path_refused")
    return value


def _open(path: Path, *, uid: int | None = None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    handle = os.fdopen(descriptor, "rb")
    info = os.fstat(handle.fileno())
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or uid is not None and info.st_uid not in {uid, 0}):
        handle.close()
        raise ValueError("backup_regular_owned_file_required")
    return handle


def _hash(handle, *, deadline: float, size_limit=MAX_ARCHIVE) -> tuple[str, int]:
    digest, count = hashlib.sha256(), 0
    while True:
        if time.monotonic() > deadline:
            raise ValueError("backup_time_limit")
        chunk = handle.read(65536)
        if not chunk:
            return digest.hexdigest(), count
        count += len(chunk)
        if count > size_limit:
            raise ValueError("backup_byte_limit")
        digest.update(chunk)


def _scan(path: Path, uid: int, *, deadline: float):
    directories, files, states, total = [], [], {}, 0
    for parent, names, leaves in os.walk(path, followlinks=False):
        for name in sorted([*names, *leaves]):
            if time.monotonic() > deadline:
                raise ValueError("backup_time_limit")
            child = Path(parent) / name
            relative = _relative(child.relative_to(path).as_posix())
            info = child.lstat()
            if child.is_symlink() or info.st_uid not in {uid, 0}:
                raise ValueError("backup_owned_nonsymlink_scope_required")
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > MAX_BYTES:
                    raise ValueError("backup_byte_limit")
                with _open(child, uid=uid) as handle:
                    checksum, size = _hash(handle, deadline=deadline, size_limit=MAX_BYTES)
                    after = os.fstat(handle.fileno())
                if size != info.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                    raise ValueError("backup_files_changed")
                files.append({"path": relative, "bytes": size, "sha256": checksum})
                states[relative] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            else:
                raise ValueError("backup_special_or_link_file_refused")
            if len(directories) + len(files) > MAX_ITEMS:
                raise ValueError("backup_item_limit")
    return sorted(directories), sorted(files, key=lambda item: item["path"]), states


def _platform(ops) -> str:
    value = ops.docker(["info", "--format", "{{.OSType}}/{{.Architecture}}"], timeout=30).stdout.strip()
    aliases = {"x86_64": "amd64", "aarch64": "arm64"}
    parts = value.split("/")
    if len(parts) != 2 or parts[0] != "linux":
        raise ValueError("linux_docker_platform_required")
    architecture = aliases.get(parts[1], parts[1])
    if architecture not in {"amd64", "arm64"}:
        raise ValueError("qualified_docker_architecture_required")
    return "linux/" + architecture


def _image(ops, reference: str, *, postgres=False) -> dict:
    raw = ops.docker(["image", "inspect", "--format", "{{json .}}", reference], timeout=30).stdout
    if len(raw) > 128 * 1024:
        raise ValueError("image_metadata_limit")
    image = _json(raw)
    if (not isinstance(image, dict) or not isinstance(image.get("Id"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image["Id"])
            or image.get("Os") != "linux" or image.get("Architecture") not in {"amd64", "arm64"}):
        raise ValueError("image_identity_required")
    digests = image.get("RepoDigests") or []
    if (not isinstance(digests, list) or len(digests) > 32
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", value)
                   for value in digests)):
        raise ValueError("image_digest_identity_invalid")
    result = {"image_id": image["Id"], "repository_digests": sorted(digests),
              "platform": image["Os"] + "/" + image["Architecture"],
              "variant": image.get("Variant") or ""}
    if not isinstance(result["variant"], str) or not re.fullmatch(r"[a-z0-9._-]{0,32}", result["variant"]):
        raise ValueError("image_variant_invalid")
    if postgres:
        configuration = image.get("Config")
        if (not isinstance(configuration, dict) or not isinstance(configuration.get("Env"), list)
                or "PG_MAJOR=18" not in configuration["Env"]):
            raise ValueError("postgres_major_image_mismatch")
    return result


def _checkout(root: Path) -> dict:
    try:
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=False, timeout=10)
        if revision.returncode or not re.fullmatch(r"[a-f0-9]{40,64}", revision.stdout.strip()):
            return {"commit": None, "dirty": None}
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, check=False, timeout=10)
        return {"commit": revision.stdout.strip(), "dirty": bool(dirty.stdout) if not dirty.returncode else None}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _require_stopped(ops, path: Path, config: dict):
    if ops.project_container_ids(path, config, running=True):
        raise ops.OperationError("Stop this installation before a cold backup. No services were changed.")
    if list((path / "postgres").rglob("postmaster.pid")):
        raise ops.OperationError("PostgreSQL has not completed a clean shutdown. Keep the installation and resolve it first.")
    if ops.private_read(path / "postgres/18/docker/PG_VERSION", limit=32).strip() != b"18":
        raise ValueError("initialized_postgres_18_required")


def _private_parent(path: Path):
    previous = os.umask(0o077)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(previous)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError("private_recovery_parent_required")


def _runtime(ops, path: Path, config: dict):
    try:
        receipt = _json(ops.private_read(path / ops.RUNTIME_IMAGES, limit=16384))
    except FileNotFoundError:
        raise ops.OperationError("Cold backup requires recorded runtime image IDs. Complete a verified start and "
                                 "stop of this installation first; no services were changed.") from None
    if (not isinstance(receipt, dict) or type(receipt.get("version")) is not int or receipt["version"] != 1
            or receipt.get("project") != config["project"] or not isinstance(receipt.get("images"), dict)
            or set(receipt["images"]) != {"app", "postgres"}
            or any(not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value)
                   for value in receipt["images"].values())):
        raise ValueError("runtime_image_receipt_invalid")
    return receipt["images"]


class _CheckingReader:
    def __init__(self, handle):
        self.handle, self.digest, self.count = handle, hashlib.sha256(), 0

    def read(self, size):
        data = self.handle.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data


def backup_installation(path: Path, config: dict, output: Path, *, ops) -> dict:
    deadline = time.monotonic() + TIME_LIMIT
    output = ops.external_path(output)
    sidecar = output.with_name(output.name + ".json")
    if output.exists() or sidecar.exists() or output.is_relative_to(path):
        raise ValueError("new_external_backup_file_required")
    _require_stopped(ops, path, config)
    platform = _platform(ops)
    recorded = _runtime(ops, path, config)
    images = {name: _image(ops, reference, postgres=name == "postgres") for name, reference in recorded.items()}
    if any(image["platform"] != platform for image in images.values()):
        raise ValueError("native_image_platform_required")
    directories, files, states = _scan(path, config["uid"], deadline=deadline)
    receipt = {"schema_version": SCHEMA, "created_at": datetime.now(UTC).isoformat(),
               "source_project": config["project"], "database": {"major": 18, "clean_shutdown": True},
               "platform": platform, "images": images, "checkout": _checkout(ops.ROOT),
               "directories": directories, "files": files,
               "restore_verified": False, "off_host_copy_verified": False, "customer_rpo_rto_qualified": False}
    metadata = json.dumps(receipt, sort_keys=True).encode()
    if len(metadata) > MAX_METADATA:
        raise ValueError("backup_metadata_limit")
    previous_umask = os.umask(0o077)
    try:
        _private_parent(output.parent)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream, tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            control = tarfile.TarInfo(CONTROL)
            control.size, control.mode = len(metadata), 0o600
            archive.addfile(control, io.BytesIO(metadata))
            for relative in directories:
                member = tarfile.TarInfo(relative)
                member.type, member.mode = tarfile.DIRTYPE, 0o700
                archive.addfile(member)
            for item in files:
                with _open(path / item["path"], uid=config["uid"]) as handle:
                    before = os.fstat(handle.fileno())
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != states[item["path"]]:
                        raise ValueError("backup_files_changed")
                    member = tarfile.TarInfo(item["path"])
                    member.size, member.mode = item["bytes"], 0o600
                    reader = _CheckingReader(handle)
                    archive.addfile(member, reader)
                    after = os.fstat(handle.fileno())
                    if (reader.count != item["bytes"] or reader.digest.hexdigest() != item["sha256"]
                            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != states[item["path"]]):
                        raise ValueError("backup_files_changed")
            _require_stopped(ops, path, config)
            if _scan(path, config["uid"], deadline=deadline)[:2] != (directories, files):
                raise ValueError("backup_files_changed")
            archive.close()
            stream.flush()
            os.fsync(stream.fileno())
        with _open(output) as handle:
            checksum, size = _hash(handle, deadline=deadline)
        result = {"schema_version": SCHEMA, "archive": output.name, "bytes": size, "sha256": checksum, "receipt": receipt}
        ops.write_private(sidecar, (json.dumps(result, sort_keys=True) + "\n").encode(), os.geteuid(), os.getegid())
        return result
    finally:
        os.umask(previous_umask)


class _BoundedFile:
    """Keep hostile extended tar headers from requesting an unbounded allocation."""
    def __init__(self, handle, *, deadline):
        self.handle, self.deadline = handle, deadline

    def read(self, size=-1):
        if size < 0 or size > MAX_METADATA or time.monotonic() > self.deadline:
            raise ValueError("archive_read_budget")
        return self.handle.read(size)

    def seek(self, offset, whence=0):
        result = self.handle.seek(offset, whence)
        if not 0 <= result <= MAX_ARCHIVE:
            raise ValueError("archive_seek_budget")
        return result

    def tell(self):
        return self.handle.tell()


def _validate_ustar_archive(handle, *, size: int, deadline: float) -> None:
    """Reject extension headers before tarfile can parse or allocate their data.

    Our writer emits only USTAR regular files and directories. Walk fixed-size
    headers and seek over payloads; Python parser/backport differences cannot
    make a PAX or GNU extension consume an attacker-selected allocation.
    """
    handle.seek(0)
    count, total = 0, 0
    while True:
        if time.monotonic() > deadline:
            raise ValueError("restore_time_limit")
        header = handle.read(512)
        if len(header) != 512:
            raise ValueError("archive_header_truncated")
        if header == bytes(512):
            if handle.read(512) != bytes(512):
                raise ValueError("archive_trailer_invalid")
            while chunk := handle.read(65536):
                if time.monotonic() > deadline or any(chunk):
                    raise ValueError("archive_trailer_invalid")
            handle.seek(0)
            return
        if header[156:157] not in {tarfile.REGTYPE, tarfile.DIRTYPE}:
            raise ValueError("archive_scope_or_type_refused")
        if header[257:265] != b"ustar\x0000":
            raise ValueError("archive_format_refused")
        try:
            member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.TarError, UnicodeError, ValueError):
            raise ValueError("archive_header_invalid") from None
        count += 1
        total += member.size
        if (count > MAX_ITEMS + 1 or member.size < 0 or total > MAX_BYTES + MAX_METADATA
                or member.isdir() and member.size != 0):
            raise ValueError("archive_header_budget")
        end = handle.tell() + (member.size + 511) // 512 * 512
        if end + 1024 > size:
            raise ValueError("archive_payload_truncated")
        handle.seek(end)


def _manifest(receipt: dict):
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA
            or receipt.get("database") != {"major": 18, "clean_shutdown": True}
            or not isinstance(receipt.get("files"), list) or not isinstance(receipt.get("directories"), list)
            or len(receipt["files"]) + len(receipt["directories"]) > MAX_ITEMS):
        raise ValueError("backup_manifest_invalid")
    directories = {_relative(value) for value in receipt["directories"]}
    if len(directories) != len(receipt["directories"]):
        raise ValueError("duplicate_backup_paths")
    files, total = {}, 0
    for item in receipt["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}
                or type(item["bytes"]) is not int or item["bytes"] < 0
                or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("backup_file_manifest_invalid")
        relative = _relative(item["path"])
        if relative in files or relative in directories:
            raise ValueError("duplicate_backup_paths")
        total += item["bytes"]
        if total > MAX_BYTES:
            raise ValueError("backup_byte_limit")
        files[relative] = item
    if not {"installation.json", "compose.yaml", "postgres/18/docker/PG_VERSION", "secrets/database_url"} <= files.keys():
        raise ValueError("portable_backup_scope_incomplete")
    for relative in [*directories, *files]:
        parent = PurePosixPath(relative).parent
        if str(parent) != "." and str(parent) not in directories:
            raise ValueError("backup_parent_manifest_missing")
    return directories, files


def restore_installation(bundle: Path, output: Path, *, port=None, public_url=None, ops) -> dict:
    bundle, output = ops.external_path(bundle), ops.external_path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("new_restore_directory_required")
    sidecar = _json(ops.private_read(bundle.with_name(bundle.name + ".json"), limit=MAX_METADATA))
    if (not isinstance(sidecar, dict) or sidecar.get("schema_version") != SCHEMA
            or type(sidecar.get("bytes")) is not int or not 1 <= sidecar["bytes"] <= MAX_ARCHIVE
            or not isinstance(sidecar.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", sidecar["sha256"])):
        raise ValueError("backup_integrity_receipt_invalid")
    details = bundle.stat()
    if details.st_mode & 0o077 or details.st_uid not in {os.getuid(), 0} and os.geteuid() != 0:
        raise ValueError("private_backup_required")
    deadline = time.monotonic() + TIME_LIMIT
    with _open(bundle) as handle:
        checksum, size = _hash(handle, deadline=deadline)
        if size != sidecar["bytes"] or checksum != sidecar["sha256"]:
            raise ValueError("backup_integrity_mismatch")
        _validate_ustar_archive(handle, size=size, deadline=deadline)
        with tarfile.open(fileobj=_BoundedFile(handle, deadline=deadline), mode="r:") as archive:
            control = archive.next()
            if control is None or control.name != CONTROL or not control.isfile() or control.size > MAX_METADATA:
                raise ValueError("backup_control_missing")
            receipt = _json(archive.extractfile(control).read(MAX_METADATA + 1))
            if receipt != sidecar.get("receipt"):
                raise ValueError("backup_integrity_receipt_mismatch")
            directories, files = _manifest(receipt)
            if receipt["platform"] != _platform(ops):
                raise ValueError("physical_backup_platform_mismatch")
            for name in ("postgres", "app"):
                recorded = receipt["images"][name]
                observed = _image(ops, recorded["image_id"], postgres=name == "postgres")
                if any(observed[key] != recorded[key] for key in ("image_id", "platform", "variant")):
                    raise ValueError("physical_backup_image_unavailable_or_changed")
            _private_parent(output.parent)
            temporary = output.parent / (".knowledge-restore-" + secrets.token_hex(8))
            temporary.mkdir(mode=0o700)
            seen = set()
            control_seen = False
            for member in archive:
                if time.monotonic() > deadline:
                    raise ValueError("restore_time_limit")
                if member.name == CONTROL:
                    if control_seen or member is not control:
                        raise ValueError("duplicate_backup_control")
                    control_seen = True  # iteration includes the first already-consumed object.
                    continue
                relative = _relative(member.name)
                if relative in seen or len(seen) >= MAX_ITEMS:
                    raise ValueError("duplicate_or_excess_archive_entries")
                seen.add(relative)
                target = temporary / (PENDING if relative == ops.MANIFEST else relative)
                if member.isdir() and relative in directories:
                    target.mkdir(mode=0o700)
                elif member.isfile() and relative in files and member.size == files[relative]["bytes"]:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    digest, written = hashlib.sha256(), 0
                    with os.fdopen(descriptor, "wb") as stream, archive.extractfile(member) as source:
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > member.size or time.monotonic() > deadline:
                                raise ValueError("restore_read_budget")
                            digest.update(chunk)
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if written != member.size or digest.hexdigest() != files[relative]["sha256"]:
                        raise ValueError("restore_file_integrity_mismatch")
                else:
                    raise ValueError("archive_scope_or_type_refused")
            if seen != directories | files.keys():
                raise ValueError("backup_manifest_coverage_mismatch")
    # No container has been started. Keep private partial output on any failure.
    config = _json(ops.private_read(temporary / PENDING))
    if (not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1
            or config.get("auth") not in {"local", "oidc"} or not isinstance(config.get("project"), str)
            or not isinstance(config.get("public_url"), str)
            or not re.fullmatch(r"knowledge-layer-[a-f0-9]{12}", config["project"])):
        raise ValueError("restored_installation_manifest_invalid")
    config.update(project="knowledge-layer-" + secrets.token_hex(6), checkout=str(ops.ROOT),
                  uid=10001 if os.geteuid() == 0 else os.getuid(), gid=10001 if os.geteuid() == 0 else os.getgid(),
                  recovery_image_id=receipt["images"]["app"]["image_id"])
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("restore_port_invalid")
        config["port"] = port
        if public_url is None and config["auth"] == "local" and config["public_url"].startswith("http:"):
            config["public_url"] = "http://127.0.0.1:" + str(port)
    config["public_url"] = ops.origin(public_url or config["public_url"], oidc=config["auth"] == "oidc")
    if type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("restore_port_invalid")
    ops.require_public_binding(config["public_url"], config["port"])
    session_invalidated = False
    owner = temporary / "state/auth/local-auth.json"
    if config["auth"] == "local" and owner.exists():
        record = _json(ops.private_read(owner, limit=8192))
        if (not isinstance(record, dict) or set(record) != {"version", "name", "salt", "hash", "session_version"}
                or type(record["version"]) is not int or record["version"] != 1
                or not isinstance(record["name"], str) or not 1 <= len(record["name"]) <= 120
                or any(not isinstance(record[key], str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                       for key in ("salt", "hash", "session_version"))):
            raise ValueError("restored_owner_record_invalid")
        record["session_version"] = secrets.token_hex(32)
        owner.unlink()
        ops.write_private(owner, (json.dumps(record) + "\n").encode(), config["uid"], config["gid"])
        session_invalidated = True
    generated = ops.compose_config(config)
    generated["services"]["postgres"]["image"] = receipt["images"]["postgres"]["image_id"]
    generated["services"]["postgres"].pop("build", None)
    for name, value in (("compose.yaml", generated), (PENDING, config)):
        target = temporary / name
        target.unlink()
        ops.write_private(target, (json.dumps(value, indent=2) + "\n").encode(), config["uid"], config["gid"])
    report = {"schema_version": "knowledge-self-host-restore-v1", "project": config["project"],
              "images": receipt["images"], "database": receipt["database"], "platform": receipt["platform"],
              "checkout": receipt["checkout"], "bundle_sha256": checksum, "filesystem_verified": True,
              "browser_sessions_invalidated": session_invalidated, "database_started": False,
              "semantic_pins_verified": False}
    (temporary / "recovery.json").unlink(missing_ok=True)
    (temporary / ops.RUNTIME_IMAGES).unlink(missing_ok=True)
    ops.write_private(temporary / "recovery.json", (json.dumps(report) + "\n").encode(), config["uid"], config["gid"])
    if os.geteuid() == 0:
        for child in [temporary, *temporary.rglob("*")]:
            os.chown(child, config["uid"], config["gid"])
    # Exclusively claim the new destination. Publish its manifest last, matching
    # init's commit marker; a partial directory cannot be started as an install.
    output.mkdir(mode=0o700)
    if os.geteuid() == 0:
        os.chown(output, config["uid"], config["gid"])
    for child in sorted(temporary.iterdir(), key=lambda item: item.name == PENDING):
        child.rename(output / (ops.MANIFEST if child.name == PENDING else child.name))
    temporary.rmdir()
    return report
