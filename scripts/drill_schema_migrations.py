"""Prove fresh upgrade, safe legacy adoption, and drift refusal in PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _postgres(*args: str, sql: str | None = None) -> None:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", *args], cwd=ROOT,
        input=sql.encode() if sql is not None else None, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Temporary PostgreSQL operation failed: {args[0]}")


def _container(secret_path: Path, *args: str, enabled_kinds: str | None = None,
               audit_dir: Path | None = None) -> tuple[int, dict[str, object]]:
    command = [
        "docker", "compose", "run", "--rm", "-T", "--no-deps",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{secret_path}:/run/migration-database-url:ro",
        "-e", "DATABASE_URL=", "-e", "DATABASE_URL_FILE=/run/migration-database-url",
    ]
    if enabled_kinds is not None:
        command.extend(["-e", f"KNOWLEDGE_ENABLED_KINDS={enabled_kinds}"])
    if audit_dir is not None:
        command.extend(["-v", f"{audit_dir}:/audit-output"])
    command.extend(["api", "python", *args])
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Migration container did not complete within 60 seconds") from exc
    try:
        payload = json.loads(result.stdout.decode().strip()) if result.stdout.strip() else {}
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Migration container returned an invalid response") from exc
    return result.returncode, payload


def _expect(secret_path: Path, action: str, exit_code: int, *, matches: bool | None = None) -> None:
    code, result = _container(secret_path, "-m", "knowledge_layer.migrate", action)
    if code != exit_code:
        raise RuntimeError(f"Migration {action} returned {code}, expected {exit_code}")
    if matches is not None and result.get("schema_matches") is not matches:
        raise RuntimeError(f"Migration {action} produced the wrong schema state")


def _expect_startup_guard(secret_path: Path, *, ready: bool) -> None:
    code, _ = _container(
        secret_path, "-c",
        "from knowledge_layer.operator_secrets import operator_secret; "
        "from knowledge_layer.store import KnowledgeStore; "
        "from knowledge_layer.migrate import ensure_current_schema; "
        "s=KnowledgeStore(database_url=operator_secret('DATABASE_URL'), initialize_schema=False); "
        "ensure_current_schema(s); s.close()",
    )
    if (code == 0) is not ready:
        raise RuntimeError("Managed startup guard accepted the wrong schema state")


def _write_secret(path: Path, user: str, password: str, database: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(f"postgresql+psycopg://{user}:{password}@postgres:5432/{database}\n")


def main() -> int:
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    user = values.get("KNOWLEDGE_DB_USER", "knowledge_layer")
    password = values["POSTGRES_PASSWORD"]
    nonce = secrets.token_hex(4)
    fresh = f"knowledge_fresh_{nonce}"
    legacy = f"knowledge_legacy_{nonce}"
    output_dir = ROOT / ".local" / "recovery"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = output_dir / f"migration-secret-{nonce}"
    audit_path = output_dir / f"audit-export-{nonce}.jsonl"
    report_path = output_dir / f"schema-drill-{nonce}.json"
    created: list[str] = []
    try:
        for database in (fresh, legacy):
            _postgres("createdb", "-U", user, "-T", "template0", database)
            created.append(database)

        _write_secret(secret_path, user, password, fresh)
        _expect(secret_path, "status", 1, matches=False)
        _expect_startup_guard(secret_path, ready=False)
        _expect(secret_path, "upgrade", 0, matches=True)
        _expect_startup_guard(secret_path, ready=True)
        _expect(secret_path, "upgrade", 0, matches=True)
        _expect(secret_path, "status", 0, matches=True)
        _expect(secret_path, "adopt", 2)
        code, source = _container(
            secret_path, "-c",
            "import json; "
            "from knowledge_layer.operator_secrets import operator_secret; "
            "from knowledge_layer.store import KnowledgeStore; "
            "s=KnowledgeStore(database_url=operator_secret('DATABASE_URL'), initialize_schema=False); "
            "print(json.dumps({'id': s.create_connection('demo', kind='local_files', "
            "name='Access drill', config={'path': '.'})['id']})); s.close()",
        )
        connection_id = source.get("id")
        if code or not isinstance(connection_id, str) or not connection_id.startswith("conn_"):
            raise RuntimeError("Could not prepare source grant fixture")
        code, _ = _container(
            secret_path, "-m", "knowledge_layer.source_access_cli", "grant",
            "--connection", connection_id, "--principal-type", "group",
            "--principal", "drill-analysts", "--role", "read",
        )
        if code:
            raise RuntimeError("Offline source grant failed")
        code, grants = _container(
            secret_path, "-m", "knowledge_layer.source_access_cli", "list",
            "--connection", connection_id,
        )
        if code or len(grants.get("grants", [])) != 1 or grants["grants"][0]["role"] != "read":
            raise RuntimeError("Offline source grant was not readable")
        probe = (
            "import json\n"
            "from knowledge_layer.access import Principal, principal_scope\n"
            "from knowledge_layer.operator_secrets import operator_secret\n"
            "from knowledge_layer.store import KnowledgeStore\n"
            "store = KnowledgeStore(database_url=operator_secret('DATABASE_URL'), "
            "initialize_schema=False, access_enforced=True)\n"
            "principal = Principal('drill-agent', frozenset({'drill-analysts'}))\n"
            "with principal_scope(principal):\n"
            "    print(json.dumps({'visible': store.count_connections('demo')}))\n"
            "store.close()\n"
        )
        code, visible = _container(secret_path, "-c", probe)
        if code or visible.get("visible") != 1:
            raise RuntimeError("PostgreSQL source grant did not authorize the principal")
        code, _ = _container(
            secret_path, "-m", "knowledge_layer.source_access_cli", "revoke",
            "--connection", connection_id, "--principal-type", "group",
            "--principal", "drill-analysts",
        )
        if code:
            raise RuntimeError("Offline source grant revocation failed")
        code, grants = _container(
            secret_path, "-m", "knowledge_layer.source_access_cli", "list",
            "--connection", connection_id,
        )
        if code or grants.get("grants") != []:
            raise RuntimeError("Offline source grant was not revoked")
        code, visible = _container(secret_path, "-c", probe)
        if code or visible.get("visible") != 0:
            raise RuntimeError("PostgreSQL source grant revocation did not take effect")
        code, exported = _container(
            secret_path, "-m", "knowledge_layer.audit_export_cli",
            "--tenant", "demo", "--connection", connection_id,
            "--output", f"/audit-output/{audit_path.name}", audit_dir=output_dir,
        )
        if code or exported.get("events") != 3 or not audit_path.is_file():
            raise RuntimeError("Operator audit export failed")
        audit_bytes = audit_path.read_bytes()
        if (exported.get("sha256") != hashlib.sha256(audit_bytes).hexdigest()
                or stat.S_IMODE(audit_path.stat().st_mode) != 0o600
                or [json.loads(line)["event_type"] for line in audit_bytes.splitlines()] !=
                ["source.create", "source.grant", "source.revoke_grant"]):
            raise RuntimeError("Operator audit export did not match recorded governance events")

        lifecycle_probe = (
            "import json\n"
            "from knowledge_layer.operator_secrets import operator_secret\n"
            "from knowledge_layer.store import KnowledgeConflict, KnowledgeStore\n"
            "from knowledge_layer.retrieval import search_knowledge\n"
            "store = KnowledgeStore(database_url=operator_secret('DATABASE_URL'), initialize_schema=False)\n"
            "source = store.create_connection('demo', kind='local_files', name='Lifecycle drill', config={})\n"
            "store.save_snapshot('demo', source['id'], {'status': 'discovery_complete', "
            "'discovery_complete': True, 'assets': [{'id': 'asset-1', 'name': 'Orders'}], "
            "'concepts': [{'id': 'concept-1', 'name': 'Orders'}], "
            "'evidence': [{'id': 'evidence-1', 'asset_id': 'asset-1', 'summary': 'Schema'}]})\n"
            "proposal = store.submit_proposal('demo', source['id'], target_concept_id='concept-1', "
            "proposed_definition='One row per order.', evidence_ids=['evidence-1'], "
            "rationale='The source owner should review this grain.')\n"
            "store.decide_proposal('demo', source['id'], proposal['id'], decision='approve', "
            "rationale='The source evidence supports this grain.')\n"
            "indexed_before = bool(search_knowledge(store, tenant_id='demo', "
            "query='One row per order')['results'])\n"
            "reviewed = store.get_snapshot('demo', source['id'])\n"
            "original_preview = store.publication_preview\n"
            "calls = [0]\n"
            "def interleave(tenant, connection):\n"
            "    calls[0] += 1\n"
            "    result = original_preview(tenant, connection)\n"
            "    if calls[0] == 1:\n"
            "        store.archive_proposals(tenant, connection, proposal_ids=[proposal['id']], "
            "reason='The owner superseded this approved proposal.')\n"
            "    return result\n"
            "store.publication_preview = interleave\n"
            "blocked = False\n"
            "try:\n"
            "    store.publish_source('demo', source['id'], expected_snapshot_id=reviewed['id'], "
            "expected_snapshot_version=reviewed['version'], "
            "rationale='The owner approves this source for agent use.')\n"
            "except KnowledgeConflict:\n"
            "    blocked = True\n"
            "current = store.get_snapshot('demo', source['id'])\n"
            "indexed_after = bool(search_knowledge(store, tenant_id='demo', "
            "query='One row per order')['results'])\n"
            "print(json.dumps({'blocked': blocked, 'agent_ready': current['agent_ready'], "
            "'proposals': len(current['proposals']), 'preview_calls': calls[0], "
            "'indexed_before': indexed_before, 'indexed_after': indexed_after}))\n"
            "store.close()\n"
        )
        code, lifecycle = _container(secret_path, "-c", lifecycle_probe)
        if (code or lifecycle != {"blocked": True, "agent_ready": False,
                                  "proposals": 0, "preview_calls": 2,
                                  "indexed_before": True, "indexed_after": False}):
            raise RuntimeError("PostgreSQL proposal archive/publication race did not fail closed")

        _write_secret(secret_path, user, password, legacy)
        code, _ = _container(
            secret_path, "-c",
            "from knowledge_layer.operator_secrets import operator_secret; "
            "from knowledge_layer.store import KnowledgeStore; "
            "from knowledge_layer.migrations.versions.v0001_initial import schema_metadata; "
            "from sqlalchemy import insert; "
            "s=KnowledgeStore(database_url=operator_secret('DATABASE_URL'), initialize_schema=False); "
            "m=schema_metadata(); m.create_all(s.engine); "
            "c=m.tables['knowledge_connections']; "
            "db=s.engine.connect(); "
            "db.execute(insert(c).values(id='conn_legacy',tenant_id='demo',kind='local_files',"
            "name='Preserved legacy record',config={'path': '.'},status='configured',"
            "created_at='2026-01-01T00:00:00Z',updated_at='2026-01-01T00:00:00Z')); "
            "db.commit(); db.close(); s.close()",
        )
        if code:
            raise RuntimeError("Could not prepare unversioned registry fixture")
        _expect(secret_path, "upgrade", 2)
        _postgres(
            "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", legacy,
            "-q", "-f", "-",
            sql="ALTER TABLE knowledge_snapshots ALTER COLUMN status TYPE varchar(16);",
        )
        _expect(secret_path, "adopt", 2)
        _postgres(
            "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", legacy,
            "-q", "-f", "-",
            sql="ALTER TABLE knowledge_snapshots ALTER COLUMN status TYPE varchar(32);",
        )
        _expect(secret_path, "adopt", 0, matches=False)
        _expect_startup_guard(secret_path, ready=False)
        _expect(secret_path, "upgrade", 0, matches=True)
        _expect_startup_guard(secret_path, ready=True)
        _expect(secret_path, "status", 0, matches=True)
        _postgres(
            "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", legacy,
            "-q", "-f", "-",
            sql="DO $$ BEGIN IF (SELECT count(*) FROM knowledge_connections) != 1 "
                "THEN RAISE EXCEPTION 'legacy record lost'; END IF; END $$;",
        )
    finally:
        secret_path.unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)
        for database in reversed(created):
            _postgres("dropdb", "-U", user, "--force", database)

    report = {
        "evaluated_at": datetime.now(UTC).isoformat(), "result": "passed",
        "fresh_upgrade": True, "idempotent_upgrade": True,
        "unversioned_upgrade_refused": True, "schema_drift_adoption_refused": True,
        "exact_adoption_preserved_data": True, "temporary_databases_dropped": True,
        "startup_guard_denied_unversioned_schema": True,
        "startup_guard_accepted_current_schema": True,
        "offline_source_grant_and_revoke": True,
        "operator_audit_export_verified": True,
        "proposal_archive_publication_race_blocked": True,
    }
    descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"result": "passed", "report": str(report_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
