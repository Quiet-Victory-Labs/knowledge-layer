"""Autonomy storage constraints preserve scoped, attributable history.

SQLite exercises portable DDL and referential effects here; it is not a live
PostgreSQL migration or backup/restore qualification.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError

from knowledge_layer.autonomy_schema import autonomy_tables
from knowledge_layer.migrations.versions import v0013_autonomy

STAMP = "2026-10-03T12:00:00+00:00"
AUTONOMY_NAMES = {
    "knowledge_autonomy_policies", "knowledge_autonomy_revisions", "knowledge_autonomy_runs",
}


def _head(tenant="tenant-a", source="source-a"):
    return {
        "tenant_id": tenant, "connection_id": source, "latest_revision": 1,
        "created_at": STAMP, "updated_at": STAMP,
    }


def _revision(tenant="tenant-a", source="source-a", revision=1):
    return {
        "tenant_id": tenant, "connection_id": source, "revision": revision,
        "payload": {"unknown": None, "empty": "", "disabled": False, "limit": 0},
        "digest": "a" * 64, "bindings": {"snapshot_version": 0, "review": None},
        "created_at": STAMP, "created_by": "approved-subject", "publication_count": 0,
    }


def _run(tenant="tenant-a", source="source-a", revision=1, **changes):
    return {
        "id": "run-a", "tenant_id": tenant, "connection_id": source,
        "policy_revision": revision, "snapshot_id": "snapshot-a", "snapshot_version": 1,
        "actor": "approved-subject", "created_at": STAMP, "owns_publication": False,
        **changes,
    }


@pytest.fixture
def database():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    tables = autonomy_tables(MetaData())
    next(iter(tables.values())).metadata.create_all(engine)
    try:
        yield engine, tables
    finally:
        engine.dispose()


def _seed(database, tenant="tenant-a", source="source-a", revision=1):
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_policies"].insert(), _head(tenant, source))
        conn.execute(tables["autonomy_revisions"].insert(), _revision(tenant, source, revision))


def test_unknown_approval_and_zero_count_roundtrip_without_invented_authority(database):
    _seed(database)
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run())
        head = conn.execute(select(tables["autonomy_policies"])).mappings().one()
        revision = conn.execute(select(tables["autonomy_revisions"])).mappings().one()
        run = conn.execute(select(tables["autonomy_runs"])).mappings().one()
    assert head["active_revision"] is None and head["active_digest"] is None
    assert head["revoked_at"] is None and head["revoked_by"] is None
    assert revision["approved_at"] is None and revision["approved_by"] is None
    assert revision["publication_count"] == 0
    assert revision["payload"] == _revision()["payload"]
    assert revision["bindings"] == _revision()["bindings"]
    assert run["owns_publication"] is False


def test_heads_and_revisions_are_scoped_by_both_tenant_and_source(database):
    _seed(database)
    _seed(database, tenant="tenant-b")
    _seed(database, source="source-b")
    engine, tables = database
    with engine.connect() as conn:
        rows = conn.execute(select(tables["autonomy_revisions"])).mappings().all()
    assert {(row["tenant_id"], row["connection_id"], row["revision"]) for row in rows} == {
        ("tenant-a", "source-a", 1), ("tenant-b", "source-a", 1), ("tenant-a", "source-b", 1),
    }


@pytest.mark.parametrize("tenant,source", [("tenant-b", "source-a"), ("tenant-a", "source-b")])
def test_revision_cannot_borrow_another_tenant_or_sources_head(database, tenant, source):
    _seed(database)
    engine, tables = database
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_revisions"].insert(), _revision(tenant, source))


@pytest.mark.parametrize("tenant,source,revision", [
    ("tenant-b", "source-a", 1), ("tenant-a", "source-b", 1), ("tenant-a", "source-a", 2),
])
def test_run_requires_its_exact_scoped_policy_revision(database, tenant, source, revision):
    _seed(database)
    engine, tables = database
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run(tenant, source, revision))


@pytest.mark.parametrize("key", ["latest_revision", "created_at", "updated_at"])
def test_head_required_metadata_cannot_be_omitted(database, key):
    engine, tables = database
    values = _head()
    del values[key]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_policies"].insert(), values)


@pytest.mark.parametrize("key", ["payload", "digest", "bindings", "created_at", "created_by", "publication_count"])
def test_revision_required_metadata_has_no_fabricated_default(database, key):
    engine, tables = database
    values = _revision()
    del values[key]
    with engine.begin() as conn:
        conn.execute(tables["autonomy_policies"].insert(), _head())
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_revisions"].insert(), values)


@pytest.mark.parametrize("key", ["snapshot_id", "snapshot_version", "actor", "created_at", "owns_publication"])
def test_run_cannot_omit_attribution_or_publication_ownership(database, key):
    _seed(database)
    engine, tables = database
    values = _run()
    del values[key]
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), values)


def test_one_publication_run_per_policy_revision_and_snapshot_version(database):
    _seed(database)
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run())
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run(id="run-b", snapshot_id="different-id"))
    with engine.begin() as conn:
        conn.execute(tables["autonomy_revisions"].insert(), _revision(revision=2))
        conn.execute(tables["autonomy_runs"].insert(), _run(revision=2, id="run-c"))
        conn.execute(tables["autonomy_runs"].insert(), _run(id="run-d", snapshot_version=2))
        rows = conn.execute(select(tables["autonomy_runs"])).mappings().all()
    assert {(row["policy_revision"], row["snapshot_version"]) for row in rows} == {
        (1, 1), (2, 1), (1, 2),
    }


def test_duplicate_run_id_is_refused_even_for_another_snapshot(database):
    _seed(database)
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run())
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run(snapshot_version=2))


def test_unreferenced_head_removal_cascades_only_its_revisions(database):
    _seed(database)
    _seed(database, tenant="tenant-b")
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_revisions"].insert(), _revision(revision=2))
        conn.execute(tables["autonomy_policies"].delete().where(
            tables["autonomy_policies"].c.tenant_id == "tenant-a",
        ))
        remaining = conn.execute(select(tables["autonomy_revisions"])).mappings().all()
    assert [(row["tenant_id"], row["revision"]) for row in remaining] == [("tenant-b", 1)]


@pytest.mark.parametrize("target", ["autonomy_policies", "autonomy_revisions"])
def test_publication_runs_prevent_policy_history_deletion(database, target):
    _seed(database)
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run())
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables[target].delete())
    with engine.connect() as conn:
        assert conn.execute(select(tables["autonomy_policies"])).mappings().one()["latest_revision"] == 1
        assert conn.execute(select(tables["autonomy_revisions"])).mappings().one()["publication_count"] == 0
        assert conn.execute(select(tables["autonomy_runs"])).mappings().one()["snapshot_id"] == "snapshot-a"


def test_v13_migration_creates_constraint_equivalent_tables_and_preserves_existing_rows(database):
    engine, _tables = database
    metadata = MetaData()
    tables = autonomy_tables(metadata)
    metadata.drop_all(engine)
    prior = Table("existing_registry_fixture", MetaData(), Column("id", Integer, primary_key=True))
    prior.create(engine)
    with engine.begin() as conn:
        conn.execute(prior.insert(), {"id": 7})
        with Operations.context(MigrationContext.configure(conn)):
            v0013_autonomy.upgrade()
        assert conn.execute(select(prior.c.id)).scalar_one() == 7
        assert set(inspect(conn).get_table_names()) == {*AUTONOMY_NAMES, prior.name}
        context = MigrationContext.configure(conn, opts={
            "compare_type": True,
            "include_name": lambda name, kind, _parents: kind != "table" or name in AUTONOMY_NAMES,
        })
        assert compare_metadata(context, metadata) == []
        conn.execute(tables["autonomy_policies"].insert(), _head())
        conn.execute(tables["autonomy_revisions"].insert(), _revision())
        conn.execute(tables["autonomy_runs"].insert(), _run())
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run(id="run-b"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(tables["autonomy_revisions"].delete())


def test_current_migration_head_follows_historical_autonomy_revision():
    config = Config()
    config.set_main_option("script_location", str(Path(v0013_autonomy.__file__).parents[1]))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0016_builds"]
    assert script.get_revision("head").down_revision == "0015_inventory_concepts"
    assert script.get_revision("0014_inventory_results").down_revision == "0013_autonomy"
    assert script.get_revision("0013_autonomy").down_revision == "0012_inventory"
    with pytest.raises(RuntimeError, match="forward-only"):
        v0013_autonomy.downgrade()


def _recovery_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "drill_postgres_recovery.py"
    spec = importlib.util.spec_from_file_location("autonomy_recovery_drill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recovery_v13_retains_prior_exact_allowlist_without_duplicates():
    recovery = _recovery_module()
    old_tables = recovery.TABLES_BY_REVISION["0012_inventory"]
    current = recovery.TABLES_BY_REVISION["0013_autonomy"]
    assert current[:-3] == old_tables
    assert set(current[-3:]) == AUTONOMY_NAMES
    assert len(current) == len(set(current))


def test_recovery_fingerprint_includes_changed_policy_and_run_records(database, monkeypatch):
    _seed(database)
    engine, tables = database
    with engine.begin() as conn:
        conn.execute(tables["autonomy_runs"].insert(), _run())
    recovery = _recovery_module()
    monkeypatch.setattr(recovery, "_docker", lambda *args: b"t" if "to_regclass" in args[-1] else b"0013_autonomy")

    def fingerprint(_user, _database, table_name):
        # Only SQL execution is adapted to SQLite; the drill's real version
        # dispatch and per-table coverage run unchanged.
        table = next((table for table in tables.values() if table.name == table_name), None)
        if table is None:
            return 0, hashlib.sha256(b"").hexdigest()
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(select(table)).mappings()]
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        return len(rows), hashlib.sha256(encoded).hexdigest()

    monkeypatch.setattr(recovery, "_stream_table_fingerprint", fingerprint)
    before = recovery._fingerprint("fixture-user", "fixture-database")
    assert before["schema_revision"] == "0013_autonomy"
    assert set(before["table_counts"]) == set(recovery.TABLES_BY_REVISION["0013_autonomy"])
    assert {name: before["table_counts"][name] for name in AUTONOMY_NAMES} == dict.fromkeys(AUTONOMY_NAMES, 1)
    with engine.begin() as conn:
        conn.execute(tables["autonomy_revisions"].update().values(publication_count=1))
        conn.execute(tables["autonomy_runs"].update().values(owns_publication=True))
    after = recovery._fingerprint("fixture-user", "fixture-database")
    assert before["table_counts"] == after["table_counts"]
    for name in ("knowledge_autonomy_revisions", "knowledge_autonomy_runs"):
        assert before["table_fingerprints"][name] != after["table_fingerprints"][name]
    assert before["table_fingerprints"]["knowledge_autonomy_policies"] == after["table_fingerprints"]["knowledge_autonomy_policies"]
