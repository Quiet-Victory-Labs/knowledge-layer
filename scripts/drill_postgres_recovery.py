"""Prove a live Compose PostgreSQL dump restores into an isolated database.

This drill never changes the source database. It creates a temporary database,
compares table counts and snapshot fingerprints, then drops that database.
The backup is removed on success unless --keep-backup is requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_TABLES = (
    "knowledge_connections", "knowledge_snapshots", "knowledge_graph_nodes",
    "knowledge_graph_edges", "knowledge_proposal_decisions",
    "knowledge_semantic_proposals", "knowledge_contract_proposals",
)
TABLES_BY_REVISION = {
    "unversioned": BASE_TABLES,
    "0001_initial": BASE_TABLES,
    "0002_source_grants": (*BASE_TABLES, "knowledge_source_grants"),
    "0003_refresh_jobs": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs"),
    "0004_source_publications": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                                 "knowledge_source_publications"),
    "0005_decision_digests": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                              "knowledge_source_publications"),
    "0006_audit_events": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                          "knowledge_source_publications", "knowledge_audit_events"),
    "0007_proposal_lifecycle": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                                 "knowledge_source_publications", "knowledge_audit_events"),
    "0008_source_paging_index": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                                  "knowledge_source_publications", "knowledge_audit_events"),
    "0009_concept_search": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                            "knowledge_source_publications", "knowledge_audit_events",
                            "knowledge_concept_search"),
    "0010_refresh_schedules": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                               "knowledge_source_publications", "knowledge_audit_events",
                               "knowledge_concept_search", "knowledge_refresh_schedules"),
    "0011_company_guidance": (*BASE_TABLES, "knowledge_source_grants", "knowledge_refresh_jobs",
                             "knowledge_source_publications", "knowledge_audit_events",
                             "knowledge_concept_search", "knowledge_refresh_schedules",
                             "knowledge_guidance_bundles", "knowledge_guidance_revisions"),
}
TABLES_BY_REVISION["0012_inventory"] = (*TABLES_BY_REVISION["0011_company_guidance"],
    "knowledge_inventory_runs", "knowledge_inventory_pages", "knowledge_inventory_entities",
    "knowledge_inventory_evidence", "knowledge_inventory_heads")
TABLES_BY_REVISION["0013_autonomy"] = (*TABLES_BY_REVISION["0012_inventory"],
    "knowledge_autonomy_policies", "knowledge_autonomy_revisions", "knowledge_autonomy_runs")
TABLES_BY_REVISION["0014_inventory_results"] = TABLES_BY_REVISION["0013_autonomy"]
TABLES_BY_REVISION["0015_inventory_concepts"] = (*TABLES_BY_REVISION["0014_inventory_results"],
    "knowledge_inventory_concept_proposals", "knowledge_inventory_concept_publications")
TABLES_BY_REVISION["0016_builds"] = (*TABLES_BY_REVISION["0015_inventory_concepts"],
    "knowledge_builds", "knowledge_build_tasks")


def _local_config() -> tuple[str, str]:
    if os.getenv("KNOWLEDGE_RECOVERY_USER") and os.getenv("KNOWLEDGE_RECOVERY_DATABASE"):
        return os.environ["KNOWLEDGE_RECOVERY_USER"], os.environ["KNOWLEDGE_RECOVERY_DATABASE"]
    path = ROOT / ".env"
    values = dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return os.getenv("KNOWLEDGE_RECOVERY_USER", values.get("KNOWLEDGE_DB_USER", "knowledge_layer")), os.getenv(
        "KNOWLEDGE_RECOVERY_DATABASE", values.get("KNOWLEDGE_DB_NAME", "knowledge_layer")
    )


def _postgres_command(*args: str) -> list[str]:
    container = os.getenv("KNOWLEDGE_RECOVERY_CONTAINER")
    return (["docker", "exec", "-i", container, *args] if container else
            ["docker", "compose", "exec", "-T", "postgres", *args])


def _docker(*args: str, input_file=None) -> bytes:
    command = _postgres_command(*args)
    result = subprocess.run(
        command, cwd=ROOT, stdin=input_file, capture_output=True, check=False,
    )
    if result.returncode:
        # Driver diagnostics can contain connection details. Never print them.
        raise RuntimeError(f"PostgreSQL recovery command failed: {args[0]}")
    return result.stdout


def _docker_to_file(output_file, *args: str) -> None:
    result = subprocess.run(
        _postgres_command(*args), cwd=ROOT,
        stdout=output_file, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"PostgreSQL backup command failed: {args[0]}")


def _fingerprint(user: str, database: str) -> dict[str, object]:
    versioned = _docker(
        "psql", "-U", user, "-d", database, "-Atc",
        "SELECT to_regclass('alembic_version') IS NOT NULL",
    ).decode("utf-8").strip() == "t"
    revision = "unversioned"
    if versioned:
        revision = _docker(
            "psql", "-U", user, "-d", database, "-Atc",
            "SELECT coalesce(string_agg(version_num, ','), 'empty') FROM alembic_version",
        ).decode("utf-8").strip()
    if revision not in TABLES_BY_REVISION:
        raise RuntimeError("Recovery drill does not recognize the source schema revision")
    counts: dict[str, int] = {}
    fingerprints: dict[str, str] = {}
    for table in TABLES_BY_REVISION[revision]:
        counts[table], fingerprints[table] = _stream_table_fingerprint(user, database, table)
    return {
        "table_counts": counts, "table_fingerprints": fingerprints,
        "snapshot_fingerprint": fingerprints["knowledge_snapshots"],
        "schema_revision": revision,
    }


def _stream_table_fingerprint(user: str, database: str, table: str) -> tuple[int, str]:
    """Hash sorted per-row digests without concatenating full metadata payloads.

    PostgreSQL can spill the small digest sort to disk. The caller streams fixed
    64 KiB blocks and counts newlines instead of buffering the entire result.
    """
    known = {name for tables in TABLES_BY_REVISION.values() for name in tables}
    if table not in known:
        raise RuntimeError("Recovery fingerprint table is outside the registry allowlist")
    sql = f"SELECT encode(sha256(convert_to(to_jsonb(t)::text, 'UTF8')), 'hex') AS row_hash FROM {table} AS t ORDER BY row_hash"
    process = subprocess.Popen(_postgres_command(
        "env", "PGOPTIONS=-c statement_timeout=60000 -c work_mem=8192 -c max_parallel_workers_per_gather=0",
        "psql", "-U", user, "-d", database, "-Atc", sql,
    ), cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    digest, count = hashlib.sha256(), 0
    try:
        for chunk in iter(lambda: process.stdout.read(65536), b""):
            digest.update(chunk)
            count += chunk.count(b"\n")
        if process.wait(timeout=10):
            raise RuntimeError("PostgreSQL recovery fingerprint failed")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    return count, digest.hexdigest()


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-backup", action="store_true", help="Retain the owner-only dump for migration")
    args = parser.parse_args()
    user, database = _local_config()
    nonce = secrets.token_hex(4)
    temporary_database = f"knowledge_restore_{nonce}"
    output_dir = ROOT / ".local" / "recovery"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = output_dir / f"backup-{nonce}.dump"
    report_path = output_dir / f"drill-{nonce}.json"
    created = False
    success = False
    try:
        source = _fingerprint(user, database)
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            _docker_to_file(handle, "pg_dump", "-U", user, "-d", database, "-Fc")
        _docker("createdb", "-U", user, "-T", "template0", temporary_database)
        created = True
        with backup.open("rb") as handle:
            _docker("pg_restore", "-U", user, "-d", temporary_database,
                    "--no-owner", "--no-acl", input_file=handle)
        restored = _fingerprint(user, temporary_database)
        if restored != source:
            raise RuntimeError("Restored database does not match source table counts and snapshots")
        _docker("dropdb", "-U", user, "--force", temporary_database)
        created = False
        report = {
            "evaluated_at": datetime.now(UTC).isoformat(),
            "result": "passed", "source_database": database,
            "temporary_database_dropped": True,
            "table_counts": source["table_counts"],
            "table_fingerprints": source["table_fingerprints"],
            "snapshot_fingerprint": source["snapshot_fingerprint"],
            "schema_revision": source["schema_revision"],
            "backup_bytes": backup.stat().st_size,
            "backup_retained": args.keep_backup,
            "backup_path": str(backup) if args.keep_backup else None,
        }
        _write_private(report_path, (json.dumps(report, indent=2) + "\n").encode("utf-8"))
        success = True
        print(json.dumps({"result": "passed", "report": str(report_path),
                          "connection_count": source["table_counts"]["knowledge_connections"],
                          "snapshot_count": source["table_counts"]["knowledge_snapshots"]}))
        return 0
    finally:
        if created:
            _docker("dropdb", "-U", user, "--force", temporary_database)
        if success and not args.keep_backup:
            backup.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
