"""Exercise a restricted PostgreSQL connector against disposable local data.

Creates a temporary database and read-only role, checks privilege filtering,
schema drift, credential rotation, and denial, then removes both. No source
rows or credentials are written to the knowledge registry.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_CHECK = """
import json
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.postgres import discover
try:
    value = discover({'secret_ref': 'KNOWLEDGE_SOURCE_DRILL', 'schema': 'drill'})
    print(json.dumps({'assets': sorted(asset['name'] for asset in value['assets']),
                      'revisions': {asset['name']: asset['revision'] for asset in value['assets']},
                      'metadata_only': all(asset['profile']['metadata_only'] for asset in value['assets'])}))
except ConnectorError as exc:
    print(json.dumps({'error': exc.code}))
"""


def _postgres(*args: str, sql: str | None = None) -> bytes:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", *args],
        cwd=ROOT, input=sql.encode() if sql is not None else None,
        capture_output=True, check=False,
    )
    if result.returncode:
        # PostgreSQL diagnostics may contain the role password. Keep them private.
        raise RuntimeError(f"Temporary PostgreSQL operation failed: {args[0]}")
    return result.stdout


def _sql(user: str, database: str, statement: str) -> None:
    _postgres("psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user,
              "-d", database, "-q", "-f", "-", sql=statement)


def _discover(secret_path: Path) -> dict[str, object]:
    command = [
        "docker", "compose", "run", "--rm", "-T", "--no-deps",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{secret_path}:/run/connector-secret:ro",
        "-e", "KNOWLEDGE_SOURCE_DRILL_FILE=/run/connector-secret",
        "api", "python", "-c", CONTAINER_CHECK,
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Connector drill container failed; credentials withheld")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Connector drill returned an invalid result") from exc


def _write_secret(path: Path, role: str, password: str, database: str) -> None:
    # Rewriting in place also models a rotated mounted secret.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(f"postgresql://{role}:{password}@postgres:5432/{database}\n")


def main() -> int:
    nonce = secrets.token_hex(4)
    database = f"knowledge_connector_{nonce}"
    role = f"knowledge_reader_{nonce}"
    password = secrets.token_hex(24)
    recovery = ROOT / ".local" / "recovery"
    recovery.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = recovery / f"connector-secret-{nonce}"
    report_path = recovery / f"connector-drill-{nonce}.json"
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    user = values.get("KNOWLEDGE_DB_USER", "knowledge_layer")
    role_created = False
    database_created = False
    try:
        _sql(user, "postgres", f"CREATE ROLE {role} LOGIN PASSWORD '{password}';")
        role_created = True
        _postgres("createdb", "-U", user, "-T", "template0", database)
        database_created = True
        _sql(user, database, "CREATE SCHEMA drill; CREATE TABLE drill.accounts (id bigint PRIMARY KEY, name text); "
             "CREATE TABLE drill.orders (id bigint PRIMARY KEY, account_id bigint); "
             f"GRANT USAGE ON SCHEMA drill TO {role}; GRANT SELECT ON drill.accounts TO {role};")
        _write_secret(secret_path, role, password, database)
        first = _discover(secret_path)
        if first.get("assets") != ["accounts"] or first.get("metadata_only") is not True:
            raise RuntimeError("Restricted reader could inspect unauthorized table metadata")

        _sql(user, database, f"GRANT SELECT ON drill.orders TO {role}; "
             "ALTER TABLE drill.accounts ADD COLUMN risk_code text;")
        changed = _discover(secret_path)
        if changed.get("assets") != ["accounts", "orders"]:
            raise RuntimeError("Granted table did not become visible")
        if changed.get("revisions", {}).get("accounts") == first.get("revisions", {}).get("accounts"):
            raise RuntimeError("Column drift did not change the schema revision")

        password = secrets.token_hex(24)
        _sql(user, "postgres", f"ALTER ROLE {role} PASSWORD '{password}';")
        stale = _discover(secret_path)
        if stale.get("error") != "database_metadata_unavailable":
            raise RuntimeError("Revoked credential still connected")
        _write_secret(secret_path, role, password, database)
        rotated = _discover(secret_path)
        if rotated.get("assets") != ["accounts", "orders"]:
            raise RuntimeError("Rotated source secret did not reconnect")

        _sql(user, database, f"REVOKE SELECT ON drill.accounts, drill.orders FROM {role};")
        denied = _discover(secret_path)
        if denied.get("assets") != []:
            raise RuntimeError("Revoked metadata privileges still exposed assets")

    finally:
        secret_path.unlink(missing_ok=True)
        if database_created:
            _postgres("dropdb", "-U", user, "--force", database)
        if role_created:
            _sql(user, "postgres", f"DROP ROLE {role};")
    report = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "result": "passed", "temporary_database_dropped": True,
        "restricted_metadata_visibility": True, "schema_drift_detected": True,
        "credential_rotation_detected": True, "credential_rotation_recovered": True,
        "grant_revocation_enforced": True, "source_rows_read": False,
    }
    descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"result": "passed", "report": str(report_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
