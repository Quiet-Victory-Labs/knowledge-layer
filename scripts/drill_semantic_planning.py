"""Qualify PostgreSQL metric planning and refresh fencing on a disposable DB.

Requires DATABASE_URL_FILE pointing to a loopback knowledge_build_* database.
Creates an isolated fixture table/read role and registry records, then removes
them. Never targets a customer database. Source SQL runs inside BEGIN READ ONLY.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path

import psycopg
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url

from knowledge_layer.jobs import RefreshQueue
from knowledge_layer.migrate import ensure_current_schema, run
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.planning import metric_plan
from knowledge_layer.refresh_fence import RefreshFenceLost, refresh_fence
from knowledge_layer.store import KnowledgeStore


def main() -> int:
    raw_url = operator_secret("DATABASE_URL")
    url = make_url(raw_url)
    if url.host not in {"127.0.0.1", "localhost"} or not (url.database or "").startswith("knowledge_build_"):
        raise RuntimeError("This drill requires an explicitly disposable loopback knowledge_build_ database")
    nonce = secrets.token_hex(6)
    table, role, password = "kl_sales_" + nonce, "kl_reader_" + nonce, secrets.token_hex(24)  # pragma: allowlist secret - generated fixture reference/value
    source_id = None
    with tempfile.TemporaryDirectory(prefix="knowledge-semantics-") as temporary:
        root = Path(temporary)
        store = KnowledgeStore(database_url=raw_url, local_files_root=root, initialize_schema=False)
        try:
            migration = run("upgrade", store)
            ensure_current_schema(store)
            with store.engine.begin() as conn:
                conn.exec_driver_sql(f'CREATE TABLE public."{table}" (order_id TEXT PRIMARY KEY, amount NUMERIC(12,2), region TEXT)')
                conn.exec_driver_sql(f'INSERT INTO public."{table}" VALUES (%s,%s,%s)',
                                     [("A", 10, "east"), ("B", 0, "east"), ("C", None, "west")])
                conn.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
                conn.exec_driver_sql(f'GRANT SELECT ON public."{table}" TO {role}')
            read_url = url.set(drivername="postgresql", username=role, password=password).render_as_string(hide_password=False)
            secret = root / "source-secret"
            secret.write_text(read_url)
            secret.chmod(0o600)
            os.environ["KNOWLEDGE_SOURCE_QUALIFICATION_FILE"] = str(secret)
            source = store.create_connection("demo", kind="postgres", name="Disposable sales fixture",
                                             config={"secret_ref": "KNOWLEDGE_SOURCE_QUALIFICATION", "schema": "public", "tables": [table]})  # pragma: allowlist secret - public revision/operator reference
            source_id = source["id"]
            snapshot = store.refresh_connection("demo", source_id)["snapshot"]
            asset = snapshot["assets"][0]
            handle = next(item["id"] for item in snapshot["evidence"] if item["evidence_type"] == "asset_metadata")
            payload = {"name": "Revenue", "description": "Recorded sales in the disposable fixture.",
                       "measure_field": "amount", "aggregation": "sum", "unit": "USD",
                       "schema_version": "metric-v2", "dimensions": ["region"], "filters": [], "time": None,
                       "cohort": {"description": "Fixture orders", "key_fields": ["order_id"]},
                       "additivity": {"kind": "additive", "non_additive_dimensions": []}}
            draft = store.submit_contract_assertion("demo", source_id, assertion_kind="metric", asset_id=asset["id"],
                payload=payload, evidence_ids=[handle], rationale="Fixture steward verifies amount, dimensions and grain.",
                expected_snapshot_id=snapshot["id"])
            store.decide_proposal("demo", source_id, draft["id"], decision="approve")
            reviewed = store.get_snapshot("demo", source_id)
            store.publish_source("demo", source_id, expected_snapshot_id=reviewed["id"],
                expected_snapshot_version=reviewed["version"], rationale="This disposable fixture metric is published for qualification.")
            plan = metric_plan(store, tenant_id="demo", connection_id=source_id, assertion_id=draft["id"], request={"group_by": ["region"]})
            with psycopg.connect(read_url) as conn, conn.cursor() as cursor:
                cursor.execute("BEGIN READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(plan["sql"], plan["parameters"])
                names = [column.name for column in cursor.description]
                rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
                cursor.execute(plan["diagnostics"]["grain_check_sql"], plan["parameters"])
                assert cursor.fetchall() == []
                conn.rollback()
                cursor.execute("SELECT has_table_privilege(current_user, %s, 'INSERT')", ("public." + table,))
                assert cursor.fetchone()[0] is False
            assert len(rows) == 2 and sum(row["selected_rows"] for row in rows) == 3
            assert sum(row["known_measure_rows"] for row in rows) == 2
            assert sum(row["missing_measure_rows"] for row in rows) == 1
            assert sum(row["zero_measure_rows"] for row in rows) == 1
            assert [row["metric_value"] for row in rows if row["dimension_0"] == "east"] == [10]
            assert [row["metric_value"] for row in rows if row["dimension_0"] == "west"] == [None]
            queue = RefreshQueue(store)
            job = queue.enqueue("demo", source_id)
            first = queue.claim("same-worker")
            with store.engine.begin() as conn:
                conn.execute(update(store.refresh_jobs).where(store.refresh_jobs.c.id == job["id"])
                             .values(lease_until="2000-01-01T00:00:00+00:00"))
            recovered = queue.claim("same-worker")
            before = store.get_snapshot("demo", source_id)
            refused = False
            with refresh_fence(job["id"], "same-worker", first["attempt"]):
                try:
                    store.refresh_connection("demo", source_id)
                except RefreshFenceLost:
                    refused = True
            assert refused and store.get_snapshot("demo", source_id)["id"] == before["id"]
            assert not queue.heartbeat(job["id"], "same-worker", attempt=first["attempt"])
            queue.cancel("demo", job["id"])
            assert queue.finish(job["id"], "same-worker", attempt=recovered["attempt"], cancelled=True)
            report = {"result": "passed", "schema_revision": migration["revision"],
                      "postgres_metric_plan_exact_rows": 2, "read_role_insert_denied": True,
                      "missing_zero_empty_distinguished": True, "grain_readback": "no_duplicates",
                      "same_worker_stale_generation_refused": True, "customer_source_access": False}
        finally:
            os.environ.pop("KNOWLEDGE_SOURCE_QUALIFICATION_FILE", None)
            if source_id:
                with store.engine.begin() as conn:
                    # Legacy graph/snapshot rows have no source FK cascade.
                    owned = [item for item in reversed(store.metadata.sorted_tables)
                             if "connection_id" in item.c and "tenant_id" in item.c]
                    for item in owned:
                        conn.execute(delete(item).where(item.c.tenant_id == "demo", item.c.connection_id == source_id))
                    conn.execute(delete(store.connections).where(store.connections.c.tenant_id == "demo",
                                                                  store.connections.c.id == source_id))
                    for item in owned:
                        assert conn.execute(select(func.count()).select_from(item).where(
                            item.c.tenant_id == "demo", item.c.connection_id == source_id)).scalar_one() == 0
            with store.engine.begin() as conn:
                conn.exec_driver_sql(f'DROP TABLE IF EXISTS public."{table}"')
                conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
            store.close()
    report["owned_fixture_metadata_removed"] = True
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
