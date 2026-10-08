"""Qualify staged discovery inventories on a disposable PostgreSQL registry.

Requires DATABASE_URL_FILE for a loopback knowledge_build_* database. Creates
only nonce-scoped fixtures: a SELECT-only source role/table and registry rows.
No customer sources, services, source promotion, or semantic certification.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from time import monotonic
from unittest.mock import patch

import psycopg
from psycopg import sql
from sqlalchemy import delete, event, func, inspect, select, update
from sqlalchemy.engine import make_url

from knowledge_layer import inventory as inventory_module
from knowledge_layer.access import Principal, principal_scope
from knowledge_layer.inventory import InventoryStore, inventory_digest
from knowledge_layer.inventory_retention import InventoryRetention
from knowledge_layer.migrate import ensure_current_schema, run
from knowledge_layer.operator_secrets import operator_secret, read_owner_only_file
from knowledge_layer.source_policy import SourcePolicy
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)

LIMITS = {"max_pages": 20, "max_entities": 100, "max_evidence": 100,
          "max_total_bytes": 1024 * 1024}
POLICY = {"id": "disposable-metadata-qualification", "revision": None}
WATERMARK = {"kind": "unknown", "value": None}
_DENIALS = (KnowledgeAccessDenied, KnowledgeNotFound)


class DrillFailed(RuntimeError):
    """A safe check identifier, never a source or database diagnostic."""


def _check(report, name, condition, **details):
    report["checks"].append({"name": name, "passed": bool(condition), **details})
    if not condition:
        raise DrillFailed(name)


def _refused(callback, exceptions):
    try:
        callback()
    except exceptions:
        return True
    return False


def _begin(inventory, tenant, source_id, scope, **kwargs):
    return inventory.begin(tenant, source_id, scope=deepcopy(scope), policy=deepcopy(POLICY),
                           watermark=deepcopy(WATERMARK), limits=deepcopy(LIMITS), **kwargs)


def _append(inventory, tenant, source_id, manifest, *, entities=(), evidence=(),
            next_cursor=None):
    return inventory.append(tenant, source_id, manifest["id"], page_index=manifest["page_count"],
        cursor=manifest["next_cursor"], next_cursor=next_cursor,
        entities=deepcopy(list(entities)), evidence=deepcopy(list(evidence)),
        **{key: manifest[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})


def _commit(inventory, tenant, source_id, manifest, **overrides):
    completion = {"listing_complete": True, "pages": manifest["page_count"],
                  "entities": manifest["entity_count"], "evidence": manifest["evidence_count"],
                  "manifest_digest": manifest["manifest_digest"], **overrides}
    return inventory.commit(tenant, source_id, manifest["id"], completion=completion)


def _current(inventory, tenant, source_id, manifest):
    return inventory.current(tenant, source_id, scope_digest=manifest["scope_digest"],
                             policy_digest=manifest["policy_digest"])


def _read_all(inventory, tenant, source_id, manifest, *, collection="entities", limit=4):
    """Follow bounded public cursors and refuse cycles or incomplete output."""
    items, cursors, after, pages = [], set(), None, 0
    while True:
        page = inventory.page(tenant, source_id, manifest["id"], collection=collection,
                              after=after, limit=limit)
        if page["text_complete"] is not True or len(page["items"]) > limit:
            raise DrillFailed("bounded_exact_readback")
        items.extend(page["items"])
        pages += 1
        after = page["next_cursor"]
        if after is None:
            return items, pages
        if after in cursors or pages >= LIMITS["max_entities"]:
            raise DrillFailed("bounded_exact_readback")
        cursors.add(after)


def _clock_state(store, tenant, source_id):
    keys = ("last_refreshed_at", "last_refresh_attempt_at", "last_complete_refresh_at")
    with store.engine.connect() as conn:
        row = conn.execute(select(*(store.connections.c[key] for key in keys)).where(
            store.connections.c.tenant_id == tenant,
            store.connections.c.id == source_id)).mappings().one()
    return dict(row)


def _records(asset):
    """Native schema plus explicitly synthetic registry fixtures; no source rows."""
    evidence = [{"id": "fixture.schema", "kind": "asset_metadata", "payload": {
        "fixture": True, "reference": asset["source_pointer"], "revision": asset["revision"],
        "source_observed_at": None}}, {"id": "fixture.definition", "kind": "document_excerpt",
        "payload": {"fixture": True, "native_version": None, "owner": None,
                    "excerpt": "Disposable fixture definitions are not enterprise facts."}}]
    entities = [{"id": "asset.fixture", "kind": "asset", "payload": {
        "fixture": True, "name": asset["name"], "pointer": asset["source_pointer"],
        "revision": asset["revision"], "schema": deepcopy(asset["schema"]),
        "row_count": asset["profile"]["row_count"]}, "evidence_ids": ["fixture.schema"]}]
    # Enough exact metadata rows to exercise write and read pagination without
    # inventing extra physical source assets or retaining business row values.
    for index in range(1, 15):
        entities.append({"id": f"definition.fixture.{index:02d}", "kind": "semantic_definition",
            "payload": {"fixture": True, "name": f"Qualification term {index}",
                "namespace": "disposable.fixture", "native_version": None,
                "definition": ("Exact fixture paragraph with Unicode: café.\n\n"
                               "A second paragraph survives registry readback. " * 12),
                "unit": None, "formula": None, "unknown": None, "measured_zero": 0,
                "observed_empty": "", "upstream_review_status": "approved"},
            "evidence_ids": ["fixture.definition"]})
    return entities, evidence


def _write_report(path, report):
    """Open once without following symlinks; validate before truncating."""
    encoded = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            raise DrillFailed("owner_only_report")
        handle.truncate(0)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _database_url():
    if not os.getenv("DATABASE_URL_FILE") or os.getenv("DATABASE_URL"):
        raise DrillFailed("requires_database_url_file_only")
    raw_url = operator_secret("DATABASE_URL")
    url = make_url(raw_url)
    if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost"}
            or not re.fullmatch(r"knowledge_build_[a-z0-9_]+", url.database or "")
            or url.port is None or not url.username or url.query
            or any(os.getenv(key) for key in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE",
                                              "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"))):
        raise DrillFailed("requires_disposable_loopback_database")
    return raw_url, url


def _census(store):
    with store.engine.connect() as conn:
        return {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for table in store.metadata.sorted_tables}


def _cleanup_registry(store, tenant, source_id):
    """Delete only the exact fixture scope, including tables without FK cascades."""
    scoped = [table for table in reversed(store.metadata.sorted_tables)
              if {"tenant_id", "connection_id"} <= set(table.c.keys())
              and table not in (store.inventory_heads, store.inventory_runs)]
    with store.engine.begin() as conn:
        run_ids = conn.execute(select(store.inventory_runs.c.id).where(
            store.inventory_runs.c.tenant_id == tenant,
            store.inventory_runs.c.connection_id == source_id)).scalars().all()
        conn.execute(delete(store.inventory_heads).where(store.inventory_heads.c.tenant_id == tenant,
                     store.inventory_heads.c.connection_id == source_id))
        conn.execute(delete(store.inventory_runs).where(store.inventory_runs.c.tenant_id == tenant,
                     store.inventory_runs.c.connection_id == source_id))
        for table in scoped:
            conn.execute(delete(table).where(table.c.tenant_id == tenant, table.c.connection_id == source_id))
        conn.execute(delete(store.connections).where(store.connections.c.tenant_id == tenant,
                     store.connections.c.id == source_id))
    with store.engine.connect() as conn:
        remaining = {table.name: conn.execute(select(func.count()).select_from(table).where(
            table.c.tenant_id == tenant, table.c.connection_id == source_id)).scalar_one()
            for table in [*scoped, store.inventory_heads, store.inventory_runs]}
        remaining[store.connections.name] = conn.execute(select(func.count()).select_from(
            store.connections).where(store.connections.c.tenant_id == tenant,
            store.connections.c.id == source_id)).scalar_one()
        for child in (store.inventory_pages, store.inventory_entities, store.inventory_evidence):
            remaining[child.name] = conn.execute(select(func.count()).select_from(child).where(
                child.c.inventory_id.in_(run_ids))).scalar_one()
    return remaining


def recover_owned_fixture(report, private_report):
    """Resolve one first-run orphan in a narrow time window before exact deletion.

    The original report lacked ownership IDs. This recovery is deliberately
    limited to that script version, exact fixture naming, and a unique result;
    future reports checkpoint exact ownership before any fixture source reads.
    """
    original = json.loads(read_owner_only_file(str(private_report)))
    if (original.get("schema_version") != "inventory-qualification-v1"
            or original.get("result") != "passed" or original.get("total_checks") != 28
            or "fixture_ownership" in original):
        raise DrillFailed("recovery_requires_original_owned_drill_report")
    raw_url, _url = _database_url()
    finished = datetime.fromtimestamp(private_report.stat().st_mtime, UTC)
    started = finished - timedelta(seconds=original["elapsed_seconds"] + 1.5)
    lower, upper = (value.isoformat().replace("+00:00", "Z") for value in
                    (started, finished + timedelta(milliseconds=100)))
    store = KnowledgeStore(database_url=raw_url, initialize_schema=False)
    try:
        ensure_current_schema(store)
        with store.engine.connect() as conn:
            rows = conn.execute(select(store.snapshots.c.tenant_id, store.snapshots.c.connection_id,
                store.snapshots.c.payload).where(store.snapshots.c.created_at >= lower,
                store.snapshots.c.created_at <= upper).limit(4)).mappings().all()
        matches = []
        for row in rows:
            payload = row["payload"]
            assets = payload.get("assets", [])
            if payload.get("kind") != "postgres" or len(assets) != 1:
                continue
            match = re.fullmatch(r"kl_inventory_([0-9a-f]{12})", assets[0].get("name", ""))
            if not match:
                continue
            tenant = "inventory_qual_" + match.group(1)
            if (row["tenant_id"] == tenant
                    and assets[0].get("source_pointer") == "postgres/public/" + match.group(0)
                    and assets[0].get("profile", {}).get("metadata_only") is True
                    and re.fullmatch(r"conn_[0-9a-f]{32}", row["connection_id"])):
                matches.append((tenant, row["connection_id"]))
        _check(report, "original_fixture_uniquely_resolved", len(matches) == 1)
        tenant, source_id = matches[0]
        report["fixture_ownership"] = {"tenant_id": tenant, "connection_id": source_id}
        remaining = _cleanup_registry(store, tenant, source_id)
        _check(report, "original_fixture_all_registry_rows_cleaned",
               all(value == 0 for value in remaining.values()), remaining_owned_rows=remaining)
    finally:
        store.close()


def _qualify_current_read_isolation(report, writer, reader, tenant, source_id, manifest,
                                   scope, entities, evidence, owner, reader_subject):
    """Commit real changes between response encoding and final registry checks."""
    original_engine = reader.engine
    with original_engine.connect() as conn:
        default_isolation = conn.get_isolation_level()
    reader.engine = original_engine.execution_options(isolation_level="REPEATABLE READ")
    durable = InventoryStore(reader)
    inventory_writer = InventoryStore(writer)
    encoded = inventory_module._encoded
    grant_keys = (writer.source_grants.c.tenant_id == tenant,
                  writer.source_grants.c.connection_id == source_id,
                  writer.source_grants.c.principal_type == "user",
                  writer.source_grants.c.principal_id == reader_subject)
    with writer.engine.connect() as conn:
        grant = dict(conn.execute(select(writer.source_grants).where(*grant_keys)).mappings().one())

    def read(operation, selected):
        if operation == "scopes":
            return durable.scopes(tenant, source_id)
        return durable.select(tenant, source_id, selected["id"],
            expected_manifest_digest=selected["manifest_digest"], entity_ids=[entities[0]["id"]])

    def complete_packet(value, operation):
        return isinstance(value, dict) and value.get("connection_id") == source_id and (
            value.get("schema_version") == "knowledge-inventory-selection-v1"
            if operation == "select" else value.get("schema_version") == "knowledge-inventory-v1"
            and "items" in value)

    try:
        with reader.engine.connect() as conn:
            _check(report, "reader_repeatable_read_configuration_applied",
                   conn.get_isolation_level() == "REPEATABLE READ")
        for operation in ("select", "scopes"):
            fired = False

            def revoke_after_encoding(value, _operation=operation):
                nonlocal fired
                body = encoded(value)
                if not fired and complete_packet(value, _operation):
                    fired = True
                    with writer.engine.begin() as conn:
                        conn.execute(delete(writer.source_grants).where(*grant_keys))
                return body

            try:
                with patch.object(inventory_module, "_encoded", revoke_after_encoding):
                    refused = _refused(lambda _operation=operation: read(_operation, manifest), _DENIALS)
                _check(report, f"repeatable_default_{operation}_observes_committed_revocation", fired and refused)
            finally:
                if fired:
                    with writer.engine.begin() as conn:
                        conn.execute(writer.source_grants.insert().values(**grant))
        for operation in ("select", "scopes"):
            fired, successor = False, None

            def replace_after_encoding(value, _operation=operation):
                nonlocal fired, successor
                body = encoded(value)
                if not fired and complete_packet(value, _operation):
                    fired = True
                    with principal_scope(owner):
                        staged = _append(inventory_writer, tenant, source_id,
                            _begin(inventory_writer, tenant, source_id, scope), entities=entities, evidence=evidence)
                        successor = _commit(inventory_writer, tenant, source_id, staged)
                return body

            with patch.object(inventory_module, "_encoded", replace_after_encoding):
                refused = _refused(lambda _operation=operation, _manifest=manifest:
                                   read(_operation, _manifest), KnowledgeConflict)
            _check(report, f"repeatable_default_{operation}_observes_committed_successor",
                   fired and successor is not None and refused)
            manifest = successor
        with reader.engine.connect() as conn:
            _check(report, "configured_reader_isolation_preserved_after_current_reads",
                   conn.get_isolation_level() == "REPEATABLE READ")
        with original_engine.connect() as conn:
            _check(report, "current_reads_do_not_change_registry_pool_default",
                   conn.get_isolation_level() == default_isolation)
        return manifest
    finally:
        reader.engine = original_engine


def _qualify_retention(report, raw_url, store, tenant, source_id, loser):
    """Real row readbacks, rollback and competing applies on owned aborted data."""
    operator = KnowledgeStore(database_url=raw_url, initialize_schema=False)
    # Local cleanup must remain available after connector approval is withdrawn.
    operator.source_policy = SourcePolicy.from_json('{"version":1,"sources":[]}')
    retention = InventoryRetention(operator)
    options = {"created_before": datetime.now(UTC), "max_runs": 1, "max_rows": 12}

    def retained_state():
        with store.engine.connect() as conn:
            heads = [dict(row) for row in conn.execute(select(store.inventory_heads).where(
                store.inventory_heads.c.tenant_id == tenant,
                store.inventory_heads.c.connection_id == source_id)
                .order_by(store.inventory_heads.c.inventory_id)).mappings()]
            runs = [dict(row) for row in conn.execute(select(store.inventory_runs).where(
                store.inventory_runs.c.tenant_id == tenant,
                store.inventory_runs.c.connection_id == source_id,
                store.inventory_runs.c.id != loser["id"])
                .order_by(store.inventory_runs.c.id)).mappings()]
        return {"heads": heads, "runs": runs, "clocks": _clock_state(store, tenant, source_id)}

    try:
        with principal_scope(None):
            before, retained = _census(store), retained_state()
            preview = retention.preview(tenant, source_id, **options)
            counts = {"runs": 1, "pages": 1, "entities": 8, "evidence": 2, "total_rows": 12}
            _check(report, "retention_preview_exact_aborted_cohort_after_approval_withdrawal",
                   preview["counts"] == counts and preview["status"] == "ready"
                   and [row["inventory_id"] for row in preview["runs"]] == [loser["id"]]
                   and preview["source_reads_executed"] is False
                   and preview["source_writes_executed"] is False
                   and preview["registry_writes_executed"] is False
                   and _census(store) == before and retained_state() == retained)
            with tempfile.TemporaryDirectory(prefix="knowledge-retention-cli-") as temporary:
                output = Path(temporary) / "preview.json"
                cli = subprocess.run([sys.executable, "-m", "knowledge_layer.inventory_retention_cli",
                    "--tenant", tenant, "--connection", source_id,
                    "--created-before", options["created_before"].isoformat(),
                    "--max-runs", "1", "--max-rows", "12", "--output", str(output)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
                cli_result = json.loads(cli.stdout) if cli.returncode == 0 else {}
                _check(report, "retention_postgres_cli_private_preview_matches_library",
                       cli.returncode == 0 and output.is_file()
                       and stat.S_IMODE(output.stat().st_mode) == 0o600
                       and json.loads(output.read_bytes()) == preview
                       and cli_result == {**preview, "receipt_file_written": True, "receipt_file_error": None}
                       and _census(store) == before and retained_state() == retained)
            apply_options = {**options, "expected_preview_digest": preview["preview_digest"]}
            with patch.object(operator, "_audit", side_effect=RuntimeError("owned_audit_failure")):
                rollback_refused = _refused(
                    lambda: retention.apply(tenant, source_id, **apply_options), RuntimeError)
            _check(report, "retention_audit_failure_rolls_back_real_rows",
                   rollback_refused and _census(store) == before
                   and retained_state() == retained
                   and retention.preview(tenant, source_id, **options) == preview)
            rendezvous = Barrier(2)

            def contender(_index):
                with principal_scope(None):
                    rendezvous.wait(timeout=10)
                    try:
                        return {"applied": retention.apply(tenant, source_id, **apply_options)}
                    except KnowledgeConflict:
                        return {"refused": True}

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(contender, range(2)))
            applied = [row["applied"] for row in results if "applied" in row]
            refused = [row for row in results if row.get("refused") is True]
            _check(report, "retention_concurrent_exact_preview_applies_once",
                   len(applied) == len(refused) == 1 and applied[0]["status"] == "applied"
                   and applied[0]["counts"] == counts
                   and applied[0]["registry_writes_executed"] is True)
            with store.engine.connect() as conn:
                remaining = {}
                for table in (store.inventory_runs, store.inventory_pages,
                              store.inventory_entities, store.inventory_evidence):
                    column = table.c.id if table is store.inventory_runs else table.c.inventory_id
                    remaining[table.name] = conn.execute(select(func.count()).select_from(table)
                        .where(column == loser["id"])).scalar_one()
                audit = [dict(row) for row in conn.execute(select(store.audit_events).where(
                    store.audit_events.c.tenant_id == tenant,
                    store.audit_events.c.connection_id == source_id,
                    store.audit_events.c.event_type == "inventory.retention")).mappings()]
            _check(report, "retention_deletes_exact_rows_and_persists_one_audit",
                   all(value == 0 for value in remaining.values()) and len(audit) == 1
                   and audit[0]["actor"] == "operator"
                   and audit[0]["details"] == {"preview_digest": preview["preview_digest"],
                       "inventory_ids": [loser["id"]], "counts": counts,
                       "created_before": preview["created_before"]})
            expected = dict(before)
            for table, count in ((store.inventory_runs, 1), (store.inventory_pages, 1),
                                 (store.inventory_entities, 8), (store.inventory_evidence, 2)):
                expected[table.name] -= count
            expected[store.audit_events.name] += 1
            _check(report, "retention_preserves_all_other_registry_rows_heads_history_and_clocks",
                   _census(store) == expected and retained_state() == retained,
                   deleted_rows=12, new_audit_events=1, registry_tables=len(expected))
            empty = retention.preview(tenant, source_id, **options)
            empty_apply = retention.apply(tenant, source_id,
                expected_preview_digest=empty["preview_digest"], **options)
            _check(report, "retention_empty_apply_no_write_and_old_preview_refused",
                   empty_apply["status"] == "empty" and empty_apply["counts"]["total_rows"] == 0
                   and empty_apply["registry_writes_executed"] is False
                   and _refused(lambda: retention.apply(tenant, source_id, **apply_options), KnowledgeConflict)
                   and _census(store) == expected and retained_state() == retained)
            cli = subprocess.run([sys.executable, "-m", "knowledge_layer.inventory_retention_cli",
                "--tenant", tenant, "--connection", source_id,
                "--created-before", options["created_before"].isoformat(),
                "--max-runs", "1", "--max-rows", "12", "--apply",
                "--expected-preview-digest", empty["preview_digest"]],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
            _check(report, "retention_postgres_cli_empty_apply_is_noop",
                   cli.returncode == 0 and json.loads(cli.stdout) == empty_apply
                   and _census(store) == expected and retained_state() == retained)
    finally:
        operator.close()


def qualify(report, *, output=None):
    raw_url, url = _database_url()
    nonce = secrets.token_hex(6)
    tenant, table, role = "inventory_qual_" + nonce, "kl_inventory_" + nonce, "kl_reader_" + nonce
    owner = Principal("inventory_owner_" + nonce, frozenset(), frozenset({"knowledge.sources.manage"}))
    reader = Principal("inventory_reader_" + nonce, frozenset())
    source_id = None
    created_fixture = False
    secret_name = "KNOWLEDGE_SOURCE_INVENTORY_" + nonce.upper()  # pragma: allowlist secret - generated fixture reference/value
    secret_variable = secret_name + "_FILE"
    report["fixture_ownership"] = {"tenant_id": tenant, "connection_id": None,
                                   "table": table, "role": role}
    if output:
        _write_report(output, report)
    with tempfile.TemporaryDirectory(prefix="knowledge-inventory-") as temporary:
        root = Path(temporary)
        store = KnowledgeStore(database_url=raw_url, local_files_root=root, initialize_schema=False)
        inventory = InventoryStore(store)
        baseline = None
        try:
            migration = run("upgrade", store)
            ensure_current_schema(store)
            expected_tables = {"knowledge_inventory_runs", "knowledge_inventory_pages",
                               "knowledge_inventory_entities", "knowledge_inventory_evidence",
                               "knowledge_inventory_heads"}
            _check(report, "migrated_postgres_current_head",
                   isinstance(migration["head"], str) and bool(migration["head"])
                   and migration["revision"] == migration["head"] and migration["schema_matches"] is True
                   and expected_tables <= set(inspect(store.engine).get_table_names()),
                   schema_revision=migration["revision"])
            baseline = _census(store)
            password = secrets.token_hex(24)
            with store.engine.begin() as conn:
                conn.exec_driver_sql(f'CREATE TABLE public."{table}" '
                                     '(record_id TEXT PRIMARY KEY, amount NUMERIC(12,2), category TEXT)')
                conn.exec_driver_sql(f'INSERT INTO public."{table}" VALUES (%s,%s,%s)',
                                     [("fixture-a", 10, "known"), ("fixture-b", 0, "zero"),
                                      ("fixture-c", None, "missing")])
                conn.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
                conn.exec_driver_sql(f'GRANT SELECT ON public."{table}" TO {role}')
            created_fixture = True
            read_url = url.set(drivername="postgresql", username=role, password=password).render_as_string(
                hide_password=False)
            secret = root / "source-secret"
            descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(read_url)
            os.environ[secret_variable] = str(secret)
            config = {"secret_ref": secret_name, "schema": "public", "tables": [table]}
            source = store.create_connection(tenant, kind="postgres", name="Disposable inventory fixture",
                                             config=config)
            source_id = source["id"]
            report["fixture_ownership"]["connection_id"] = source_id
            if output:
                _write_report(output, report)
            snapshot = store.refresh_connection(tenant, source_id)["snapshot"]
            asset = snapshot["assets"][0]
            _check(report, "native_read_only_metadata_discovery",
                   len(snapshot["assets"]) == 1 and len(asset["schema"]) == 3
                   and asset["profile"]["metadata_only"] is True
                   and asset["profile"]["row_count"] is None
                   and asset["profile"]["profile_rows"] == 0,
                   native_assets=1, native_fields=3, retained_source_rows=0)
            with psycopg.connect(read_url, connect_timeout=5) as conn, conn.cursor() as cursor:
                cursor.execute("BEGIN READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(sql.SQL("SELECT count(*), count(amount), count(*) FILTER (WHERE amount IS NULL), "
                    "count(*) FILTER (WHERE amount=0) FROM public.{}").format(sql.Identifier(table)))
                source_counts_before = cursor.fetchone()
                cursor.execute("SELECT has_table_privilege(current_user, %s, 'INSERT'), "
                               "has_table_privilege(current_user, %s, 'UPDATE'), "
                               "has_table_privilege(current_user, %s, 'DELETE')",
                               ("public." + table,) * 3)
                source_permissions = cursor.fetchone()
                conn.rollback()
                source_read_only_refused = _refused(
                    lambda: cursor.execute(sql.SQL("INSERT INTO public.{} (record_id) VALUES (%s)").format(
                        sql.Identifier(table)), ("forbidden-fixture",)), psycopg.errors.InsufficientPrivilege)
                conn.rollback()
            _check(report, "source_account_writes_refused", source_read_only_refused
                   and source_permissions == (False, False, False)
                   and source_counts_before == (3, 2, 1, 1),
                   source_rows=3, known_amount_rows=2, missing_amount_rows=1, zero_amount_rows=1)
            for subject, grant in ((owner.subject, "manage"), (reader.subject, "read")):
                store.grant_source_access(tenant, source_id, principal_type="user", principal_id=subject,
                                          role=grant)
            store.source_policy = SourcePolicy.from_json(json.dumps({"version": 1, "sources": [{
                "tenant_id": tenant, "kind": "postgres", "selector": config,
                "allowed_subjects": [owner.subject, reader.subject], "allowed_groups": []}]}))
            store.access_enforced = True
            scope = {"namespace": "public", "selectors": [table]}
            clocks_before = _clock_state(store, tenant, source_id)
            entities, evidence = _records(asset)
            with principal_scope(owner):
                initial = _begin(inventory, tenant, source_id, scope,
                                 expected_counts={"pages": 3, "entities": 15, "evidence": 2})
                _check(report, "unknown_inventory_not_complete_empty",
                       _current(inventory, tenant, source_id, initial) is None
                       and initial["status"] == "staging" and initial["completed_at"] is None
                       and initial["watermark"] == WATERMARK)
                first = _append(inventory, tenant, source_id, initial, entities=entities[:5],
                                next_cursor="fixture.page.2")
                replay = inventory.append(tenant, source_id, initial["id"], page_index=0, cursor=None,
                    next_cursor="fixture.page.2", entities=deepcopy(entities[:5]), evidence=[],
                    **{key: initial[key] for key in ("scope_digest", "policy_digest", "watermark_digest")})
                _check(report, "exact_page_replay_idempotent", replay["replayed"] is True
                       and all(replay[key] == first[key] for key in
                               ("page_count", "entity_count", "evidence_count", "byte_count", "manifest_digest")))
                changed = deepcopy(entities[:5])
                changed[0]["payload"]["name"] = "Changed replay fixture"
                _check(report, "changed_replay_refused", _refused(lambda: inventory.append(
                    tenant, source_id, initial["id"], page_index=0, cursor=None, next_cursor="fixture.page.2",
                    entities=changed, evidence=[], **{key: initial[key] for key in
                        ("scope_digest", "policy_digest", "watermark_digest")}), KnowledgeConflict))
                second = _append(inventory, tenant, source_id, first, entities=entities[5:10],
                                 next_cursor="fixture.page.3")
                staged = _append(inventory, tenant, source_id, second, entities=entities[10:], evidence=evidence)
                _check(report, "staged_pages_leave_head_unchanged", _current(inventory, tenant, source_id, staged) is None)
                _check(report, "incomplete_listing_refused", _refused(
                    lambda: _commit(inventory, tenant, source_id, staged, listing_complete=False),
                    KnowledgeValidationError))
            with principal_scope(reader):
                _check(report, "reader_cannot_read_staging", _refused(
                    lambda: inventory.page(tenant, source_id, staged["id"]), _DENIALS))
                _check(report, "reader_cannot_mutate_inventory", _refused(
                    lambda: _begin(inventory, tenant, source_id, scope), _DENIALS))
            with principal_scope(owner):
                committed = _commit(inventory, tenant, source_id, staged)
            # A new store/pool proves committed rows survived beyond the write connection.
            reader_store = KnowledgeStore(database_url=raw_url, initialize_schema=False, access_enforced=True)
            reader_store.source_policy = store.source_policy
            try:
                with principal_scope(reader):
                    durable = InventoryStore(reader_store)
                    observed, read_pages = _read_all(durable, tenant, source_id, committed)
                    observed_evidence, evidence_pages = _read_all(durable, tenant, source_id, committed,
                                                                 collection="evidence", limit=1)
                    expected = [{**item, "digest": inventory_digest(item)} for item in sorted(entities, key=lambda item: item["id"])]
                    expected_evidence = [{**item, "digest": inventory_digest(item)} for item in sorted(evidence, key=lambda item: item["id"])]
                    _check(report, "durable_exact_paged_readback", observed == expected
                           and observed_evidence == expected_evidence and read_pages > 1 and evidence_pages > 1,
                           staged_pages=3, entity_read_pages=read_pages, evidence_read_pages=evidence_pages,
                           exact_entities=len(observed), exact_evidence=len(observed_evidence))
                    _check(report, "complete_inventory_is_not_semantic_approval",
                           committed["compatibility"]["agent_ready"] is False
                           and committed["compatibility"]["meaning_certified"] is False
                           and committed["compatibility"]["legacy_snapshot"] is False)
                    scopes = durable.scopes(tenant, source_id)
                    _check(report, "reader_discovers_current_scope_without_hashes",
                           len(scopes["items"]) == 1 and scopes["next_cursor"] is None
                           and scopes["items"][0]["id"] == committed["id"]
                           and scopes["items"][0]["manifest_digest"] == committed["manifest_digest"]
                           and scopes["meaning_certified"] is False
                           and scopes["source_reads_executed"] is False)
                    wanted = [entities[0]["id"], entities[-1]["id"]]
                    streamed_cursors = []

                    def observe_selection_cursor(_conn, cursor, _statement, _parameters, context, _many):
                        if context.execution_options.get("yield_per") == 1:
                            streamed_cursors.append(bool(getattr(cursor, "name", None)))

                    event.listen(reader_store.engine, "after_cursor_execute", observe_selection_cursor)
                    try:
                        selection = durable.select(tenant, source_id, committed["id"],
                            expected_manifest_digest=committed["manifest_digest"], entity_ids=wanted)
                    finally:
                        event.remove(reader_store.engine, "after_cursor_execute", observe_selection_cursor)
                    _check(report, "selection_uses_actual_bounded_postgres_server_cursors",
                           streamed_cursors == [True, True])
                    _check(report, "exact_current_selection_resolves_cross_page_evidence",
                           selection["entities"] == [item for item in expected if item["id"] in wanted]
                           and selection["evidence"] == expected_evidence
                           and selection["inventory"]["generation"] == committed["generation"]
                           and selection["inventory"]["manifest_digest"] == committed["manifest_digest"]
                           and selection["text_complete"] is True
                           and selection["source_text_authority"] == "untrusted_evidence"
                           and selection["meaning_certified"] is False
                           and selection["agent_ready"] is False
                           and selection["source_reads_executed"] is False,
                           selected_entities=len(selection["entities"]),
                           exact_evidence=len(selection["evidence"]))
                    committed = _qualify_current_read_isolation(report, store, reader_store,
                        tenant, source_id, committed, scope, entities, evidence, owner, reader.subject)
            finally:
                reader_store.close()
            with principal_scope(owner):
                runs = [_append(inventory, tenant, source_id,
                                _begin(inventory, tenant, source_id, scope), entities=entities[:8], evidence=evidence)
                        for _ in range(2)]
                with store.engine.connect() as conn:
                    bases = conn.execute(select(store.inventory_runs.c.base_generation).where(
                        store.inventory_runs.c.id.in_([item["id"] for item in runs]))).scalars().all()
                _check(report, "competing_runs_share_generation", bases == [committed["generation"]] * 2)
                _check(report, "replacement_staging_keeps_current_head",
                       _current(inventory, tenant, source_id, runs[0])["id"] == committed["id"])
                rendezvous = Barrier(2)

                def contender(manifest):
                    with principal_scope(owner):
                        rendezvous.wait(timeout=10)
                        try:
                            return {"winner": _commit(inventory, tenant, source_id, manifest)}
                        except KnowledgeConflict:
                            return {"loser": manifest}

                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(contender, runs))
                winners = [item["winner"] for item in results if "winner" in item]
                losers = [item["loser"] for item in results if "loser" in item]
                _check(report, "atomic_postgres_generation_winner_loser", len(winners) == len(losers) == 1)
                winner, loser = winners[0], losers[0]
                head = _current(inventory, tenant, source_id, winner)
                _check(report, "loser_cannot_advance_head_or_clock", head["id"] == winner["id"]
                       and head["generation"] == committed["generation"] + 1
                       and head["last_complete_scope_at"] == winner["last_complete_scope_at"]
                       and inventory.status(tenant, source_id, loser["id"])["status"] == "staging")
                inventory.abort(tenant, source_id, loser["id"])
                removed, _ = _read_all(inventory, tenant, source_id, winner, collection="removed")
                _check(report, "comparable_complete_removal_exact",
                       [item["id"] for item in removed] == sorted(item["id"] for item in entities[8:]),
                       removed_entities=len(removed))
                historical = inventory.page(tenant, source_id, committed["id"])
                _check(report, "history_retained_and_labeled", historical["current"] is False
                       and historical["status"] == "committed")
                _check(report, "superseded_selection_refused",
                       _refused(lambda: inventory.select(tenant, source_id, committed["id"],
                           expected_manifest_digest=committed["manifest_digest"],
                           entity_ids=[entities[0]["id"]]), KnowledgeConflict))
                empty = _begin(inventory, tenant, source_id, scope,
                               expected_counts={"pages": 1, "entities": 0, "evidence": 0})
                _check(report, "empty_staging_does_not_remove_current",
                       _current(inventory, tenant, source_id, empty)["id"] == winner["id"])
                empty = _append(inventory, tenant, source_id, empty)
                empty = _commit(inventory, tenant, source_id, empty)
                empty_rows, _ = _read_all(inventory, tenant, source_id, empty)
                removed_empty, _ = _read_all(inventory, tenant, source_id, empty, collection="removed")
                _check(report, "explicit_complete_empty_is_known_zero", empty_rows == []
                       and empty["entity_count"] == empty["evidence_count"] == 0
                       and empty["completed_at"] is not None and len(removed_empty) == 8,
                       verified_empty_entities=0, removed_entities=len(removed_empty))
                alternate_scope = {"namespace": "public.alternate-test-scope", "selectors": [table]}
                other = _append(inventory, tenant, source_id,
                                _begin(inventory, tenant, source_id, alternate_scope))
                other = _commit(inventory, tenant, source_id, other)
                _check(report, "incomparable_scope_does_not_infer_removal", _refused(
                    lambda: inventory.page(tenant, source_id, other["id"], collection="removed"),
                    KnowledgeConflict))
                before_error = _current(inventory, tenant, source_id, empty)
                with store.engine.begin() as conn:
                    source_status = conn.execute(select(store.connections.c.status).where(
                        store.connections.c.id == source_id)).scalar_one()
                    conn.execute(update(store.connections).where(store.connections.c.id == source_id)
                                 .values(status="error"))
                try:
                    _check(report, "failed_source_blocks_retained_inventory", _refused(
                        lambda: _current(inventory, tenant, source_id, empty), KnowledgeConflict))
                finally:
                    with store.engine.begin() as conn:
                        conn.execute(update(store.connections).where(store.connections.c.id == source_id)
                                     .values(status=source_status))
                _check(report, "failed_read_does_not_advance_inventory_clock",
                       _current(inventory, tenant, source_id, empty) == before_error)
                _check(report, "inventory_does_not_stamp_source_refresh_or_publication",
                       _clock_state(store, tenant, source_id) == clocks_before
                       and store.get_snapshot(tenant, source_id)["id"] == snapshot["id"]
                       and store.get_snapshot(tenant, source_id)["agent_ready"] is False)
            _qualify_retention(report, raw_url, store, tenant, source_id, loser)
            store.access_enforced = False
            store.revoke_source_access(tenant, source_id, principal_type="user", principal_id=reader.subject)
            store.access_enforced = True
            with principal_scope(reader):
                _check(report, "revoked_grant_blocks_current_and_history",
                       _refused(lambda: inventory.page(tenant, source_id, committed["id"]), _DENIALS)
                       and _refused(lambda: _current(inventory, tenant, source_id, empty), _DENIALS))
                _check(report, "revoked_grant_blocks_scope_discovery_and_selection",
                       _refused(lambda: inventory.scopes(tenant, source_id), _DENIALS)
                       and _refused(lambda: inventory.select(tenant, source_id, empty["id"],
                           expected_manifest_digest=empty["manifest_digest"],
                           entity_ids=[entities[0]["id"]]), _DENIALS))
            with psycopg.connect(read_url, connect_timeout=5) as conn, conn.cursor() as cursor:
                cursor.execute("BEGIN READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(sql.SQL("SELECT count(*), count(amount), count(*) FILTER (WHERE amount IS NULL), "
                    "count(*) FILTER (WHERE amount=0) FROM public.{}").format(sql.Identifier(table)))
                _check(report, "registry_writes_leave_source_fixture_unchanged",
                       cursor.fetchone() == source_counts_before)
                conn.rollback()
            report["qualification"] = {"backend": "postgresql", "disposable_database": True,
                "native_metadata_assets": 1, "native_metadata_fields": 3, "fixture_inventory_entities": 15,
                "source_row_values_retained": False, "customer_source_access": False,
                "semantic_approval": False, "services_started": 0}
        finally:
            os.environ.pop(secret_variable, None)
            try:
                if source_id:
                    leftovers = _cleanup_registry(store, tenant, source_id)
                    _check(report, "owned_registry_rows_cleaned", all(value == 0 for value in leftovers.values()),
                           remaining_owned_rows=leftovers)
                if created_fixture:
                    with store.engine.begin() as conn:
                        conn.exec_driver_sql(f'DROP TABLE IF EXISTS public."{table}"')
                        conn.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
                    with store.engine.connect() as conn:
                        fixture_remaining = conn.exec_driver_sql(
                            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname='public' AND c.relname=%s), "
                            "EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s)", (table, role)).one()
                    _check(report, "owned_source_fixture_cleaned", tuple(fixture_remaining) == (False, False))
                if baseline is not None:
                    after = _census(store)
                    _check(report, "all_registry_table_totals_unchanged", after == baseline,
                           registry_tables=len(baseline), before=baseline, after=after)
            finally:
                store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write safe JSON to an existing parent directory, mode 600")
    parser.add_argument("--recover-report", type=Path,
                        help="Clean uniquely resolved legacy metadata from this drill's original private report")
    arguments = parser.parse_args()
    report = {"schema_version": "inventory-qualification-v1", "result": "failed", "checks": [],
              "claim_limits": ["Disposable source and registry fixtures; no customer database accessed.",
                               "Paged discovery persistence is separate from semantic approval.",
                               "Native schema metadata and synthetic definitions exercise storage, not business truth."]}
    started = monotonic()
    try:
        if arguments.recover_report:
            recover_owned_fixture(report, arguments.recover_report)
        else:
            qualify(report, output=arguments.output)
        report["result"] = "passed"
    except Exception as exc:  # noqa: BLE001 -- sanitize arbitrary driver diagnostics at CLI boundary
        # Driver diagnostics can contain connection information. Never stringify
        # an arbitrary exception or emit a traceback, URL, source row, or secret.
        report["failure"] = {"type": type(exc).__name__}
        if isinstance(exc, DrillFailed):
            report["failure"]["check"] = exc.args[0]
    report["elapsed_seconds"] = round(monotonic() - started, 3)
    report["passed_checks"] = sum(item["passed"] for item in report["checks"])
    report["total_checks"] = len(report["checks"])
    if arguments.output:
        try:
            _write_report(arguments.output, report)
        except (OSError, DrillFailed, ValueError, TypeError):
            print(json.dumps({"result": "failed", "failure": "owner_only_report_write_refused"}))
            return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
