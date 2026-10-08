"""Explicit, guarded PostgreSQL schema operations for self-hosted installs.

Usage: python -m knowledge_layer.migrate status|upgrade|adopt
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from knowledge_layer.migrations.versions.v0001_initial import revision as BASELINE_REVISION
from knowledge_layer.migrations.versions.v0001_initial import schema_metadata as baseline_metadata
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.store import KnowledgeStore


class SchemaMigrationError(RuntimeError):
    """A safe operator-facing migration failure."""


def _config(connection: Connection, store: KnowledgeStore) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    config.attributes["connection"] = connection
    config.attributes["target_metadata"] = store.metadata
    return config


def _revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _schema_differences(connection: Connection, metadata) -> list:
    context = MigrationContext.configure(connection, opts={
        "compare_type": True,
        "include_name": lambda name, type_, _parents: type_ != "table" or name in metadata.tables,
    })
    return compare_metadata(context, metadata)


def ensure_current_schema(store: KnowledgeStore) -> None:
    """Refuse OIDC traffic when schema revision or physical tables diverge."""
    if store.backend != "postgresql":
        raise SchemaMigrationError("The managed schema requires PostgreSQL")
    with store.engine.connect() as connection:
        config = _config(connection, store)
        head = ScriptDirectory.from_config(config).get_current_head()
        if _revision(connection) != head:
            raise SchemaMigrationError("Database schema revision is not current; run the migration command")
        if _schema_differences(connection, store.metadata):
            raise SchemaMigrationError("Database schema differs from the installed application metadata")


def run(action: str, store: KnowledgeStore) -> dict[str, object]:
    if store.backend != "postgresql":
        raise SchemaMigrationError("Schema operations require PostgreSQL")
    if action not in {"status", "upgrade", "adopt"}:
        raise SchemaMigrationError("Unknown schema operation")
    with store.engine.begin() as connection:
        if action != "status":
            # Serialize concurrent Helm/Compose migration attempts for this DB.
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('knowledge_layer_schema_migration'))"
            )
        config = _config(connection, store)
        head = ScriptDirectory.from_config(config).get_current_head()
        current = _revision(connection)
        existing = set(inspect(connection).get_table_names())
        knowledge_tables = set(store.metadata.tables)
        if action == "upgrade":
            if current is None and existing & knowledge_tables:
                raise SchemaMigrationError(
                    "Existing registry tables are unversioned; validate and run adopt before upgrade"
                )
            command.upgrade(config, "head")
        elif action == "adopt":
            if current is not None:
                raise SchemaMigrationError("The database already has a schema revision")
            expected = set(baseline_metadata().tables)
            if not expected.issubset(existing):
                raise SchemaMigrationError("The existing registry is incomplete; do not stamp it")
            if (existing & set(store.metadata.tables)) - expected:
                raise SchemaMigrationError("The unversioned registry contains tables beyond the frozen baseline")
            if _schema_differences(connection, baseline_metadata()):
                raise SchemaMigrationError("Existing registry schema differs from the frozen baseline")
            command.stamp(config, BASELINE_REVISION)
        current = _revision(connection)
        compatible = current == head and not _schema_differences(connection, store.metadata)
        return {
            "action": action,
            "revision": current,
            "head": head,
            "schema_matches": compatible,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "upgrade", "adopt"))
    args = parser.parse_args()
    url = operator_secret("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL or DATABASE_URL_FILE is required")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        result = run(args.action, store)
    except SchemaMigrationError as exc:
        parser.exit(2, f"Schema operation refused: {exc}\n")
    except Exception:  # noqa: BLE001 - never print driver diagnostics containing a database URL.
        parser.exit(2, "Schema operation failed; inspect private database logs and verify the backup\n")
    finally:
        store.close()
    print(json.dumps(result))
    return 0 if args.action == "adopt" or result["schema_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
