"""Schema baseline stays separate from application startup side effects."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from knowledge_layer.migrate import SchemaMigrationError, ensure_current_schema
from knowledge_layer.migrations.versions.v0001_initial import schema_metadata
from knowledge_layer.store import KnowledgeStore


def test_registry_can_initialize_without_creating_tables_and_baseline_matches():
    store = KnowledgeStore(database_url="sqlite:///:memory:", initialize_schema=False)
    assert inspect(store.engine).get_table_names() == []
    schema_metadata().create_all(store.engine)
    with store.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert any(item[0] == "add_table" and item[1].name == "knowledge_source_grants"
                   for item in compare_metadata(context, store.metadata))
    store.source_grants.create(store.engine)
    store.refresh_schedules.create(store.engine)
    store.refresh_jobs.create(store.engine)
    store.publications.create(store.engine)
    store.audit_events.create(store.engine)
    store.concept_search.create(store.engine)
    store.guidance_bundles.create(store.engine)
    store.guidance_revisions.create(store.engine)
    for table_name in ("inventory_runs", "inventory_pages", "inventory_entities", "inventory_evidence", "inventory_heads"):
        getattr(store, table_name).create(store.engine)
    for table_name in ("autonomy_policies", "autonomy_revisions", "autonomy_runs"):
        getattr(store, table_name).create(store.engine)
    for table_name in ("inventory_concept_proposals", "inventory_concept_publications"):
        getattr(store, table_name).create(store.engine)
    for table_name in ("builds", "build_tasks"):
        getattr(store, table_name).create(store.engine)
    with store.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE knowledge_connections ADD COLUMN last_refresh_attempt_at VARCHAR(40)")
        connection.exec_driver_sql("ALTER TABLE knowledge_connections ADD COLUMN last_complete_refresh_at VARCHAR(40)")
        connection.exec_driver_sql("ALTER TABLE knowledge_proposal_decisions ADD COLUMN proposal_digest VARCHAR(64)")
        for table, index in (
            ("knowledge_semantic_proposals", "ix_knowledge_semantic_proposals_active_submitter"),
            ("knowledge_contract_proposals", "ix_knowledge_contract_proposals_active_submitter"),
        ):
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN submitted_by VARCHAR(255) NOT NULL DEFAULT 'legacy'"
            )
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN archived_at VARCHAR(40)")
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN archived_by VARCHAR(255)")
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN archive_reason VARCHAR(1000)")
            connection.exec_driver_sql(
                f"CREATE INDEX {index} ON {table} (tenant_id, connection_id, submitted_by, archived_at)"
            )
        connection.exec_driver_sql(
            "CREATE INDEX ix_knowledge_connections_tenant_created_id "
            "ON knowledge_connections (tenant_id, created_at, id)"
        )
    with store.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, store.metadata) == []
    store.close()


def test_managed_schema_rejects_sqlite():
    store = KnowledgeStore(database_url="sqlite:///:memory:", initialize_schema=False)
    try:
        try:
            ensure_current_schema(store)
        except SchemaMigrationError as exc:
            assert "PostgreSQL" in str(exc)
        else:
            raise AssertionError("Managed schema accepted SQLite")
    finally:
        store.close()
