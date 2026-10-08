"""Owner-only PostgreSQL logical backup with optional isolated restore verification.

Never restores over the running registry or prints database diagnostics. Restore
verification compares every public registry table and requires a quiescent source.
Off-host replication and alert delivery remain the operator's responsibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

USER = DATABASE = "knowledge_layer"


def compose(install: Path, *args: str) -> list[str]:
    return ["docker", "compose", "--project-directory", str(install),
            "-f", str(install / "compose.yaml"), "exec", "-T", "postgres", *args]


def command(install: Path, *args: str, **kwargs) -> bytes:
    result = subprocess.run(compose(install, *args), capture_output=True, timeout=600,
                            check=False, **kwargs)
    if result.returncode:
        raise RuntimeError("database_command_failed")
    return result.stdout


def fingerprint(install: Path, database: str) -> dict:
    deadline = monotonic() + 600
    tables = command(install, "psql", "-U", USER, "-d", database, "-v", "ON_ERROR_STOP=1", "-Atc",
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").decode().splitlines()
    if (not 1 <= len(tables) <= 100 or "alembic_version" not in tables
            or any(not re.fullmatch(r"(?:knowledge_[a-z_]+|alembic_version)", name) for name in tables)):
        raise RuntimeError("registry_table_scope_refused")
    result = {}
    for table in tables:
        sql = ("SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') AS row_hash "
               f'FROM "{table}" AS t ORDER BY row_hash')
        with subprocess.Popen(compose(install, "env", "PGOPTIONS=-c statement_timeout=60000 -c work_mem=8192",
                "psql", "-U", USER, "-d", database, "-v", "ON_ERROR_STOP=1", "-Atc", sql),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            digest, rows = hashlib.sha256(), 0
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            raise RuntimeError("registry_fingerprint_timeout")
                        if not selector.select(timeout=min(remaining, 10)):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        digest.update(chunk)
                        rows += chunk.count(b"\n")
                if process.wait(timeout=min(10, max(0.01, deadline - monotonic()))):
                    raise RuntimeError("registry_fingerprint_failed")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        result[table] = {"rows": rows, "sha256": digest.hexdigest()}
    return result


def backup(install: Path, output: Path, verify: bool = False) -> dict:
    install, output = install.absolute(), output.absolute()
    if (install.resolve() != install or not (install / "compose.yaml").is_file()
            or output.resolve() != output):
        raise ValueError("regular_install_and_backup_paths_required")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077 or output.stat().st_uid != os.geteuid():
        raise ValueError("owner_only_backup_directory_required")
    if shutil.disk_usage(output).free < 1024**3:
        raise ValueError("backup_free_space_required")
    started, at = monotonic(), datetime.now(UTC)
    name = at.strftime("registry-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    dump = output / f"{name}.dump"
    descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        result = subprocess.run(compose(install, "pg_dump", "-U", USER, "-d", DATABASE, "-Fc"),
            stdout=stream, stderr=subprocess.DEVNULL, timeout=600, check=False)
    if result.returncode or dump.stat().st_size == 0:
        raise RuntimeError("backup_failed_private_partial_retained")
    digest = hashlib.sha256()
    with dump.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    report = {"schema_version": "knowledge-registry-backup-v1", "started_at": at.isoformat(),
              "backup": dump.name, "bytes": dump.stat().st_size, "sha256": digest.hexdigest(),
              "backup_seconds": monotonic() - started, "restore_verified": False,
              "off_host_copy_verified": False, "customer_rpo_rto_qualified": False}
    if verify:
        temporary, created = "knowledge_restore_" + secrets.token_hex(6), False
        restore_started = monotonic()
        try:
            expected = fingerprint(install, DATABASE)
            command(install, "createdb", "-U", USER, "-T", "template0", temporary)
            created = True
            with dump.open("rb") as stream:
                command(install, "pg_restore", "-U", USER, "-d", temporary,
                        "--no-owner", "--no-acl", "--exit-on-error", stdin=stream)
            observed = fingerprint(install, temporary)
            if observed != expected:
                raise RuntimeError("restore_mismatch_or_nonquiescent_source")
            report.update(restore_verified=True, table_fingerprints=observed,
                          restore_seconds=monotonic() - restore_started)
        finally:
            if created:
                command(install, "dropdb", "-U", USER, "--force", temporary)
        report["temporary_database_removed"] = True
    report["finished_at"] = datetime.now(UTC).isoformat()
    fd = os.open(output / f"{name}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-restore", action="store_true")
    parser.add_argument("--quiescent", action="store_true", help="Confirm API and worker writers are stopped")
    args = parser.parse_args(argv)
    if args.verify_restore and not args.quiescent:
        parser.error("Restore verification requires an explicitly quiescent registry")
    try:
        report = backup(args.install, args.output, args.verify_restore)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, "Registry backup refused or failed; inspect protected backup storage\n")
    print(json.dumps({key: report[key] for key in
                     ("schema_version", "bytes", "backup_seconds", "restore_verified")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
