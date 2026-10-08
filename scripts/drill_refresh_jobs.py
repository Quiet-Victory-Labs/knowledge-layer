"""Prove a queued refresh survives request exit and runs in a separate container."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime

from drill_schema_migrations import ROOT, _container, _expect, _postgres, _write_secret


def main() -> int:
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    user = values.get("KNOWLEDGE_DB_USER", "knowledge_layer")
    password = values["POSTGRES_PASSWORD"]
    nonce = secrets.token_hex(4)
    database = f"knowledge_jobs_{nonce}"
    output_dir = ROOT / ".local" / "recovery"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = output_dir / f"jobs-secret-{nonce}"
    report_path = output_dir / f"jobs-drill-{nonce}.json"
    created = False
    try:
        _postgres("createdb", "-U", user, "-T", "template0", database)
        created = True
        _write_secret(secret_path, user, password, database)
        _expect(secret_path, "upgrade", 0, matches=True)
        prepare = (
            "import json\n"
            "from knowledge_layer.jobs import RefreshQueue\n"
            "from knowledge_layer.operator_secrets import operator_secret\n"
            "from knowledge_layer.store import KnowledgeStore\n"
            "store = KnowledgeStore(database_url=operator_secret('DATABASE_URL'), initialize_schema=False)\n"
            "source = store.create_connection('demo', kind='local_files', name='Durable jobs drill', "
            "config={'path': 'juniper', 'max_files': 10})\n"
            "job = RefreshQueue(store).enqueue('demo', source['id'])\n"
            "print(json.dumps({'source_id': source['id'], 'job_id': job['id'], 'status': job['status']}))\n"
            "store.close()\n"
        )
        code, submitted = _container(secret_path, "-c", prepare)
        source_id = submitted.get("source_id")
        job_id = submitted.get("job_id")
        if code or submitted.get("status") != "queued" or not isinstance(source_id, str) or not isinstance(job_id, str):
            raise RuntimeError("Could not persist a refresh request")
        code, _ = _container(secret_path, "-m", "knowledge_layer.worker", "run", "--once",
                             enabled_kinds="local_files")
        if code:
            raise RuntimeError("Separate refresh worker failed")
        inspect = (
            "import json\n"
            "from knowledge_layer.jobs import RefreshQueue\n"
            "from knowledge_layer.operator_secrets import operator_secret\n"
            "from knowledge_layer.store import KnowledgeStore\n"
            "store = KnowledgeStore(database_url=operator_secret('DATABASE_URL'), initialize_schema=False)\n"
            f"job = RefreshQueue(store).get('demo', {job_id!r})\n"
            f"snapshot = store.get_snapshot('demo', {source_id!r})\n"
            "print(json.dumps({'job_status': job['status'], 'attempt': job['attempt'], "
            "'job_snapshot_id': job['result_snapshot_id'], "
            "'snapshot_id': (snapshot or {}).get('id'), "
            "'asset_count': len((snapshot or {}).get('assets', []))}))\n"
            "store.close()\n"
        )
        code, result = _container(secret_path, "-c", inspect)
        if (code or result.get("job_status") != "succeeded" or result.get("attempt") != 1
                or result.get("job_snapshot_id") != result.get("snapshot_id")
                or not result.get("asset_count")):
            raise RuntimeError("Separate worker did not persist a successful snapshot")
    finally:
        secret_path.unlink(missing_ok=True)
        if created:
            _postgres("dropdb", "-U", user, "--force", database)
    report = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "result": "passed", "queued_request_survived_process_exit": True,
        "separate_worker_completed": True, "snapshot_id_matched_job": True,
        "asset_count": result["asset_count"], "attempt": result["attempt"],
        "temporary_database_dropped": True,
    }
    descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"result": "passed", "report": str(report_path),
                      "asset_count": report["asset_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
