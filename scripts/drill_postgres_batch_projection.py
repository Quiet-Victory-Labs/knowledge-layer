"""Exercise bounded source-list projection in a disposable migrated PostgreSQL database."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import json
import os
from sqlalchemy import event
from sqlalchemy.engine import make_url
from knowledge_layer.migrate import run
from knowledge_layer.store import KnowledgeStore

database = os.environ['KNOWLEDGE_BATCH_DB']
url = make_url(os.environ['DATABASE_URL']).set(database=database)
store = KnowledgeStore(database_url=url.render_as_string(hide_password=False), initialize_schema=False)
migration = run('upgrade', store)
assert migration['schema_matches'] is True
ids = [store.create_connection('demo', kind='local_files', name=f'Source {index:02d}', config={})['id']
       for index in range(50)]
for source_id in ids:
    store.save_snapshot('demo', source_id, {
        'status': 'discovery_complete', 'discovery_complete': True,
        'assets': [{'id': 'asset-1', 'name': 'Orders'}],
        'evidence': [{'id': 'evidence-1', 'asset_id': 'asset-1', 'summary': 'Schema'}],
    })
statements = []
def record(_connection, _cursor, statement, _parameters, _context, _many):
    statements.append(statement)
event.listen(store.engine, 'before_cursor_execute', record)
page = store.page_connections('demo', limit=50)
event.remove(store.engine, 'before_cursor_execute', record)
assert len(page['connections']) == page['total'] == 50
assert len(statements) <= 25
assert all(row == store.get_connection('demo', row['id']) for row in page['connections'])
print(json.dumps({'result': 'passed', 'sources': 50, 'sql_statements': len(statements),
                  'schema_revision': migration['revision']}))
store.close()
"""


def _db_user() -> str:
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return values.get("KNOWLEDGE_DB_USER", "knowledge_layer")


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, check=False, timeout=90,
    )
    if result.returncode:
        raise RuntimeError("PostgreSQL batch projection drill failed; inspect private container logs")
    return result.stdout.decode("utf-8")


def main() -> int:
    user = _db_user()
    database = f"knowledge_batch_{secrets.token_hex(4)}"
    created = False
    try:
        _docker("exec", "-T", "postgres", "createdb", "-U", user, "-T", "template0", database)
        created = True
        raw = _docker(
            "run", "--rm", "-T", "--no-deps", "-e", f"KNOWLEDGE_BATCH_DB={database}",
            "api", "python", "-c", PROBE,
        )
        result = json.loads(raw.strip())
        if result.get("result") != "passed" or result.get("sources") != 50:
            raise RuntimeError("The PostgreSQL batch projection drill did not pass")
        _docker("exec", "-T", "postgres", "dropdb", "-U", user, "--force", database)
        created = False
        output_dir = ROOT / ".local" / "recovery"
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = output_dir / f"batch-projection-{secrets.token_hex(4)}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({**result, "evaluated_at": datetime.now(UTC).isoformat(),
                       "temporary_database_dropped": True}, handle, indent=2)
            handle.write("\n")
        print(json.dumps({**result, "report": str(path)}))
        return 0
    finally:
        if created:
            _docker("exec", "-T", "postgres", "dropdb", "-U", user, "--force", database)


if __name__ == "__main__":
    raise SystemExit(main())
