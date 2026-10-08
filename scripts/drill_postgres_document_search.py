"""Qualify cross-source evidence search on a disposable large PostgreSQL registry."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = '''
import json
import resource
from time import perf_counter
from alembic import command
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.engine import make_url
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.contracts import search_document_evidence
from knowledge_layer import retrieval
from knowledge_layer.migrate import _config, run
from knowledge_layer.store import KnowledgeStore
import os

database = os.environ['KNOWLEDGE_SEARCH_DB']
url = make_url(os.environ['DATABASE_URL']).set(database=database)
store = KnowledgeStore(database_url=url.render_as_string(hide_password=False), initialize_schema=False)
with store.engine.begin() as conn:
    command.upgrade(_config(conn, store), '0008_source_paging_index')
with store.engine.begin() as conn:
    conn.execute(text("""
        INSERT INTO knowledge_connections
            (id, tenant_id, kind, name, config, status, created_at, updated_at)
        SELECT 'conn_search_' || lpad(g::text, 8, '0'), 'demo', 'local_files',
               'Synthetic source ' || g, '{}'::json, 'configured',
               '2026-10-01T00:00:00.000000Z', '2026-10-01T00:00:00.000000Z'
        FROM generate_series(1, 20000) AS g
    """))
    conn.execute(text("""
        INSERT INTO knowledge_source_grants
            (tenant_id, connection_id, principal_type, principal_id, role, granted_at, granted_by)
        SELECT 'demo', 'conn_search_' || lpad(g::text, 8, '0'),
               'user', 'reader', 'read', '2026-10-01T00:00:00.000000Z', 'operator'
        FROM generate_series(4, 20000, 4) AS g
        UNION ALL
        SELECT 'demo', 'conn_search_00000001', 'user', 'reader', 'read',
               '2026-10-01T00:00:00.000000Z', 'operator'
    """))
    conn.execute(text("""
        INSERT INTO knowledge_snapshots
            (row_id, tenant_id, connection_id, version, snapshot_id, status, created_at, payload)
        SELECT 'snap_search_' || lpad(g::text, 8, '0'), 'demo',
               'conn_search_' || lpad(g::text, 8, '0'), 1,
               'snapshot_search_' || lpad(g::text, 8, '0'), 'discovery_complete',
               '2026-10-01T00:00:00.000000Z',
               json_build_object(
                   'status', 'discovery_complete', 'discovery_complete', true,
                   'concepts', json_build_array(json_build_object(
                       'id', 'equipment-concept', 'name', 'Equipment maintenance',
                       'status', 'candidate')),
                   'evidence', json_build_array(json_build_object(
                       'id', 'equipment-term', 'evidence_type', 'document_derived_term',
                       'summary', 'Equipment maintenance schedule',
                       'source_pointer', 'equipment.md')))
        FROM generate_series(3, 20000) AS g
    """))
    conn.execute(text("""
        UPDATE knowledge_snapshots
        SET payload = jsonb_set(payload::jsonb, '{assets}', (
            SELECT jsonb_agg(jsonb_build_object(
                'id', 'asset-' || n, 'description', repeat('x', 9000)))
            FROM generate_series(1, 500) n
        ))::json
        WHERE connection_id BETWEEN 'conn_search_00000100' AND 'conn_search_00000109'
    """))
    max_snapshot_bytes = conn.execute(text("""
        SELECT max(octet_length(payload::text)) FROM knowledge_snapshots
        WHERE connection_id BETWEEN 'conn_search_00000100' AND 'conn_search_00000109'
    """)).scalar_one()
    assert 4_000_000 < max_snapshot_bytes < 5 * 1024 * 1024
    for source_id, proposal_id, definition, archived in (
        ('conn_search_00000004', 'p_active', 'Distinctive cobalt margin rule', None),
        ('conn_search_00000008', 'p_archived', 'Retired amber margin rule',
         '2026-10-01T01:00:00Z'),
    ):
        conn.execute(insert(store.proposals).values(
            id=proposal_id, tenant_id='demo', connection_id=source_id,
            proposal_type='semantic_correction', target_concept_id='equipment-concept',
            proposed_definition=definition, proposed_mapping=None, evidence_ids=['equipment-term'],
            rationale='Synthetic reviewed fixture.', status='pending_review',
            created_at='2026-10-01T00:00:00Z', submitted_by='fixture', archived_at=archived,
        ))
        conn.execute(insert(store.decisions).values(
            tenant_id='demo', connection_id=source_id, proposal_id=proposal_id,
            decision='approve', decided_at='2026-10-01T00:30:00Z',
            proposal_digest='synthetic-fixture',
        ))
    row = conn.execute(select(store.snapshots.c.payload).where(
        store.snapshots.c.connection_id == 'conn_search_00000008',
    )).scalar_one()
    row['concepts'][0].update({
        'definition': 'Retired amber margin rule', 'definition_proposal_id': 'p_archived',
    })
    row['proposals'] = [{
        'id': 'p_archived', 'target_concept_id': 'equipment-concept',
        'proposed_definition': 'Retired amber margin rule',
    }]
    conn.execute(update(store.snapshots).where(
        store.snapshots.c.connection_id == 'conn_search_00000008',
    ).values(payload=row))
start_upgrade = perf_counter()
assert run('upgrade', store)['schema_matches'] is True
upgrade_ms = round((perf_counter() - start_upgrade) * 1000, 1)
migration_peak_rss_mib = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
assert migration_peak_rss_mib < 512
with store.engine.connect() as conn:
    active_text = conn.execute(select(store.concept_search.c.search_text).where(
        store.concept_search.c.connection_id == 'conn_search_00000004',
    )).scalar_one()
    archived_text = conn.execute(select(store.concept_search.c.search_text).where(
        store.concept_search.c.connection_id == 'conn_search_00000008',
    )).scalar_one()
    assert 'Distinctive cobalt margin rule' in active_text
    assert 'Retired amber margin rule' not in archived_text
target = 'conn_search_00000001'
hidden = 'conn_search_00000002'
for source_id, evidence_id in ((target, 'authorized-policy'), (hidden, 'private-policy')):
    store.save_snapshot('demo', source_id, {
        'status': 'discovery_complete', 'discovery_complete': True,
        'concepts': [{'id': 'billable-concept', 'name': 'Billable labor policy',
                      'status': 'candidate', 'evidence_ids': [evidence_id]}],
        'evidence': [{'id': evidence_id, 'evidence_type': 'document_derived_term',
                      'summary': 'Billable labor policy', 'source_pointer': 'policy.md'}],
    })
with store.engine.begin() as conn:
    conn.execute(text('ANALYZE knowledge_connections'))
    conn.execute(text('ANALYZE knowledge_source_grants'))
    conn.execute(text('ANALYZE knowledge_snapshots'))
    conn.execute(text('ANALYZE knowledge_concept_search'))
    assert conn.execute(text('SELECT count(*) FROM knowledge_concept_search')).scalar_one() == 20000
store.access_enforced = True
with store.engine.connect() as conn:
    concept_plan = conn.execute(text("""
        EXPLAIN (FORMAT JSON)
        SELECT row_id FROM knowledge_concept_search
        WHERE tenant_id = 'demo'
          AND to_tsvector('simple'::regconfig, search_text)
              @@ plainto_tsquery('simple'::regconfig, 'Billable labor policy')
        LIMIT 257
    """)).scalar_one()
    if isinstance(concept_plan, str):
        concept_plan = json.loads(concept_plan)
    assert 'ix_knowledge_concept_search_text_gin' in json.dumps(concept_plan)
    plan = conn.execute(text("""
        EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
        SELECT c.id, c.name, ranked.snapshot_id, ranked.payload -> 'evidence'
        FROM knowledge_connections c
        JOIN (
            SELECT connection_id, snapshot_id, payload,
                   row_number() OVER (PARTITION BY connection_id ORDER BY version DESC) AS source_rank
            FROM knowledge_snapshots WHERE tenant_id = 'demo'
        ) ranked ON ranked.connection_id = c.id AND ranked.source_rank = 1
        WHERE c.tenant_id = 'demo'
          AND EXISTS (
              SELECT 1 FROM knowledge_source_grants g
              WHERE g.tenant_id = 'demo' AND g.connection_id = c.id
                AND g.principal_type = 'user' AND g.principal_id = 'reader'
          )
    """)).scalar_one()
    if isinstance(plan, str):
        plan = json.loads(plan)
    sql_execution_ms = round(plan[0]['Execution Time'], 1)
statements = []
def record(_connection, _cursor, statement, _parameters, _context, _many):
    statements.append(statement)
with principal_scope(Principal('reader', frozenset())):
    start = perf_counter()
    count = 0
    last_id = None
    for connection in store.iter_connections('demo'):
        count += 1
        last_id = connection['id']
    stream_ms = round((perf_counter() - start) * 1000, 1)
    assert count == 5001 and last_id == target
    retrieval._semantic_scores = lambda _texts, _query: None
    start = perf_counter()
    concept_result = retrieval.search_knowledge(store, tenant_id='demo', query='Billable labor policy')
    concept_search_ms = round((perf_counter() - start) * 1000, 1)
    assert concept_result['scanned_concepts'] == 1
    assert concept_result['semantic_candidates'] == 1
    assert concept_result['search_mode'] == 'indexed_lexical'
    assert concept_result['index_limit_reached'] is False
    assert concept_result['results'][0]['connection_id'] == target
    assert concept_result['results'][0]['concept_id'] == 'billable-concept'
    start = perf_counter()
    broad = retrieval.search_knowledge(store, tenant_id='demo', query='Equipment maintenance')
    broad_search_ms = round((perf_counter() - start) * 1000, 1)
    assert broad['index_limit_reached'] is True
    assert broad['scanned_concepts'] == retrieval.MAX_SEMANTIC_CANDIDATES
    assert all(row['connection_id'] != hidden for row in broad['results'])
    event.listen(store.engine, 'before_cursor_execute', record)
    start = perf_counter()
    result = search_document_evidence(store, tenant_id='demo', query='billable labor')
    search_ms = round((perf_counter() - start) * 1000, 1)
    event.remove(store.engine, 'before_cursor_execute', record)
    original_get = store.get_connection
    def revoke_before_final(tenant_id, source_id):
        with store.engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM knowledge_source_grants
                WHERE tenant_id = 'demo' AND connection_id = :source_id
                  AND principal_type = 'user' AND principal_id = 'reader'
            """), {'source_id': source_id})
        return original_get(tenant_id, source_id)
    store.get_connection = revoke_before_final
    revoked_search = retrieval.search_knowledge(
        store, tenant_id='demo', query='Billable labor policy')
    assert revoked_search['results'] == []
assert result['total_matches'] == 1
assert len(result['matches']) == 1
assert result['matches'][0]['connection_id'] == target
assert result['matches'][0]['evidence_ref']['evidence_id'] == 'authorized-policy'
assert len(statements) <= 3
print(json.dumps({'result': 'passed', 'sources': 20000, 'document_snapshots': 20000,
                  'concepts': 20000, 'searched_concepts': concept_result['scanned_concepts'],
                  'reader_grants': 5001,
                  'streamed_sources': count, 'stream_ms': stream_ms,
                  'concept_search_ms': concept_search_ms,
                  'broad_concept_search_ms': broad_search_ms, 'v8_to_v9_upgrade_ms': upgrade_ms,
                  'migration_peak_rss_mib': migration_peak_rss_mib,
                  'max_backfill_snapshot_bytes': max_snapshot_bytes,
                  'concept_gin_used': True,
                  'grant_revocation_race_blocked': True,
                  'sql_execution_ms': sql_execution_ms,
                  'document_search_ms': search_ms, 'document_search_sql_statements': len(statements),
                  'matches': result['total_matches']}))
store.close()
'''


def _db_user() -> str:
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return values.get("KNOWLEDGE_DB_USER", "knowledge_layer")


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, check=False, timeout=180,
    )
    if result.returncode:
        raise RuntimeError("PostgreSQL document search drill failed; inspect private container logs")
    return result.stdout.decode("utf-8")


def main() -> int:
    database = f"knowledge_search_{secrets.token_hex(4)}"
    user = _db_user()
    created = False
    try:
        _docker("exec", "-T", "postgres", "createdb", "-U", user, "-T", "template0", database)
        created = True
        raw = _docker("run", "--rm", "-T", "--no-deps", "-e", f"KNOWLEDGE_SEARCH_DB={database}",
                      "api", "python", "-c", PROBE)
        result = json.loads(raw.strip())
        if result.get("result") != "passed" or result.get("sources") != 20_000:
            raise RuntimeError("The PostgreSQL document search drill did not pass")
        _docker("exec", "-T", "postgres", "dropdb", "-U", user, "--force", database)
        created = False
        output_dir = ROOT / ".local" / "recovery"
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = output_dir / f"document-search-{secrets.token_hex(4)}.json"
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
