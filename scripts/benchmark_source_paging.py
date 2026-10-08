"""Compare PostgreSQL connection paging plans before and after the seek index.

Uses a disposable synthetic database and drops it even when the benchmark fails.
The timings describe this one local host, not a production service objective.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROW_COUNT = 20_000
INDEX = "ix_knowledge_connections_tenant_created_id"
CREATED_AT = "2026-10-01T00:00:00.000000Z"


def _db_user() -> str:
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return values.get("KNOWLEDGE_DB_USER", "knowledge_layer")


def _postgres(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", *args],
        cwd=ROOT, capture_output=True, check=False, timeout=60,
    )
    if result.returncode:
        # Do not print database diagnostics, which may include operator details.
        raise RuntimeError(f"PostgreSQL paging benchmark failed during {args[0]}")
    return result.stdout.decode("utf-8")


def _sql(user: str, database: str, statement: str) -> str:
    return _postgres(
        "psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1",
        "-U", user, "-d", database, "-c", statement,
    ).strip()


def _plan(user: str, database: str, query: str) -> dict[str, object]:
    result = json.loads(_sql(user, database, f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}"))[0]
    plan = result["Plan"]
    return {
        "execution_ms": result["Execution Time"],
        "planning_ms": result["Planning Time"],
        "rows": plan["Actual Rows"],
        "plan": plan,
    }


def _index_nodes(node: dict[str, object]) -> list[str]:
    names = [str(node["Index Name"])] if "Index Name" in node else []
    for child in node.get("Plans", []):
        names.extend(_index_nodes(child))
    return names


def main() -> int:
    user = _db_user()
    database = f"knowledge_paging_{secrets.token_hex(4)}"
    created = False
    try:
        _postgres("createdb", "-U", user, "-T", "template0", database)
        created = True
        _sql(user, database, """
            CREATE TABLE knowledge_connections (
                id varchar(64) PRIMARY KEY, tenant_id varchar(64) NOT NULL,
                kind varchar(32) NOT NULL, name varchar(120) NOT NULL,
                config json NOT NULL, status varchar(32) NOT NULL,
                created_at varchar(40) NOT NULL, updated_at varchar(40) NOT NULL,
                last_refreshed_at varchar(40), last_error text
            );
            CREATE INDEX ix_knowledge_connections_tenant_id
                ON knowledge_connections (tenant_id);
            CREATE TABLE knowledge_source_grants (
                tenant_id varchar(64) NOT NULL, connection_id varchar(64) NOT NULL,
                principal_type varchar(16) NOT NULL, principal_id varchar(255) NOT NULL,
                role varchar(16) NOT NULL,
                PRIMARY KEY (tenant_id, connection_id, principal_type, principal_id)
            );
            INSERT INTO knowledge_connections
                (id, tenant_id, kind, name, config, status, created_at, updated_at)
            SELECT 'conn_bench_' || lpad(g::text, 8, '0'), 'demo', 'local_files',
                'Synthetic source ' || g, '{}', 'configured',
                '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z'
            FROM generate_series(1, 20000) AS g;
            INSERT INTO knowledge_source_grants
            SELECT 'demo', 'conn_bench_' || lpad(g::text, 8, '0'), 'user', 'reader', 'read'
            FROM generate_series(4, 20000, 4) AS g;
            ANALYZE knowledge_connections;
            ANALYZE knowledge_source_grants;
        """)
        condition = (
            "c.tenant_id = 'demo' AND "
            f"(c.created_at, c.id) < ('{CREATED_AT}', 'conn_bench_00010000')"
        )
        queries = {
            "first_page": "SELECT c.id FROM knowledge_connections c WHERE c.tenant_id = 'demo' "
                          "ORDER BY c.created_at DESC, c.id DESC LIMIT 50",
            "deep_page": f"SELECT c.id FROM knowledge_connections c WHERE {condition} "
                         "ORDER BY c.created_at DESC, c.id DESC LIMIT 50",
            "granted_deep_page": (
                f"SELECT c.id FROM knowledge_connections c WHERE {condition} AND EXISTS ("
                "SELECT 1 FROM knowledge_source_grants g WHERE g.tenant_id = 'demo' "
                "AND g.connection_id = c.id AND g.principal_type = 'user' "
                "AND g.principal_id = 'reader') "
                "ORDER BY c.created_at DESC, c.id DESC LIMIT 50"
            ),
        }
        before_ids = {name: _sql(user, database, query).splitlines() for name, query in queries.items()}
        before = {name: _plan(user, database, query) for name, query in queries.items()}
        _sql(user, database, f"CREATE INDEX {INDEX} ON knowledge_connections (tenant_id, created_at, id)")
        _sql(user, database, "ANALYZE knowledge_connections")
        after_ids = {name: _sql(user, database, query).splitlines() for name, query in queries.items()}
        after = {name: _plan(user, database, query) for name, query in queries.items()}
        if (any(item["rows"] != 50 for item in [*before.values(), *after.values()])
                or before_ids != after_ids):
            raise RuntimeError("The synthetic paging query changed or did not return a full page")
        if any(INDEX not in _index_nodes(item["plan"]) for item in after.values()):
            raise RuntimeError("The PostgreSQL planner did not use the new paging index")
        _postgres("dropdb", "-U", user, "--force", database)
        created = False
        report = {
            "evaluated_at": datetime.now(UTC).isoformat(),
            "result": "passed", "synthetic_rows": ROW_COUNT,
            "reader_grants": ROW_COUNT // 4,
            "measurements": {
                name: {
                    "before_ms": before[name]["execution_ms"],
                    "after_ms": after[name]["execution_ms"],
                    "after_index_used": INDEX in _index_nodes(after[name]["plan"]),
                    "before_plan": before[name]["plan"],
                    "after_plan": after[name]["plan"],
                } for name in queries
            },
            "temporary_database_dropped": True,
        }
        output_dir = ROOT / ".local" / "recovery"
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = output_dir / f"paging-benchmark-{secrets.token_hex(4)}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        print(json.dumps({
            "result": "passed", "report": str(path),
            "timings_ms": {name: [before[name]["execution_ms"], after[name]["execution_ms"]]
                           for name in queries},
            "index_used": {name: INDEX in _index_nodes(after[name]["plan"]) for name in queries},
        }))
        return 0
    finally:
        if created:
            _postgres("dropdb", "-U", user, "--force", database)


if __name__ == "__main__":
    raise SystemExit(main())
