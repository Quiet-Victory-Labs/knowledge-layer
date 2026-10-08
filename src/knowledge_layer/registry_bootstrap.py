"""One-time least-privilege role bootstrap for a new, dedicated Cloud SQL registry.

Run as a temporary operator job, with bootstrap and application URLs injected
from separate Secret Manager secrets. Never mount the bootstrap secret in the app.
This refuses an existing application role or a populated database.
"""

from __future__ import annotations

import json
import re

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from knowledge_layer.operator_secrets import operator_secret


class BootstrapDatabaseError(RuntimeError):
    """A safe operation label and SQLSTATE, without server diagnostics or SQL."""

    def __init__(self, stage: str, sqlstate: str | None) -> None:
        self.stage = stage
        self.sqlstate = sqlstate if sqlstate and re.fullmatch(r"[0-9A-Z]{5}", sqlstate) else "unavailable"
        super().__init__("Dedicated registry bootstrap database operation refused")


def _execute(
    cursor: psycopg.Cursor,
    query: str | sql.Composable,
    parameters: tuple[object, ...] | None = None,
    *,
    stage: str,
) -> None:
    try:
        cursor.execute(query, parameters)
    except psycopg.Error as exc:
        raise BootstrapDatabaseError(stage, exc.sqlstate) from None


def bootstrap() -> dict[str, object]:
    root = make_url(operator_secret("KNOWLEDGE_BOOTSTRAP_DATABASE_URL") or "")
    app = make_url(operator_secret("DATABASE_URL") or "")
    if (root.username != "postgres" or app.username != "knowledge_layer"
            or root.database != "knowledge_layer" or app.database != root.database
            or root.host != app.host or dict(root.query) != dict(app.query)
            or root.port != app.port or not root.password or not app.password
            or root.drivername != "postgresql+psycopg" or app.drivername != root.drivername
            or root.host is not None or root.port is not None
            or set(root.query) != {"host"}
            or not isinstance(root.query["host"], str)
            or not re.fullmatch(r"/cloudsql/[a-z][a-z0-9-]{4,29}:[a-z0-9-]+:[a-z0-9-]+", root.query["host"])):
        raise ValueError("Dedicated registry bootstrap inputs are invalid")
    # psycopg uses libpq's URI scheme, while SQLAlchemy includes the driver.
    connection_url = root.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(connection_url, connect_timeout=10) as connection, connection.cursor() as cursor:
        _execute(cursor, "SELECT current_database()", stage="database_identity")
        if cursor.fetchone() != ("knowledge_layer",):
            raise ValueError("Connected registry identity does not match the bootstrap target")
        _execute(cursor, "SELECT 1 FROM pg_roles WHERE rolname = %s", (app.username,), stage="role_absence")
        if cursor.fetchone() is not None:
            raise ValueError("Application role already exists; use the rotation runbook")
        _execute(cursor, """
            SELECT 1 FROM pg_namespace WHERE nspname <> 'public'
                AND nspname <> 'information_schema' AND left(nspname, 3) <> 'pg_'
            UNION ALL
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
            UNION ALL
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
            UNION ALL
            SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql'
            UNION ALL SELECT 1 FROM pg_largeobject_metadata
            UNION ALL SELECT 1 FROM pg_event_trigger
            UNION ALL SELECT 1 FROM pg_foreign_server
            UNION ALL SELECT 1 FROM pg_foreign_data_wrapper
            UNION ALL SELECT 1 FROM pg_publication
            UNION ALL SELECT 1 FROM pg_subscription
            UNION ALL SELECT 1 FROM pg_default_acl
            UNION ALL SELECT 1 FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_conversion c JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_opclass o JOIN pg_namespace n ON n.oid = o.opcnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_opfamily o JOIN pg_namespace n ON n.oid = o.opfnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_ts_config o JOIN pg_namespace n ON n.oid = o.cfgnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_ts_dict o JOIN pg_namespace n ON n.oid = o.dictnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_ts_parser o JOIN pg_namespace n ON n.oid = o.prsnamespace
                WHERE n.nspname = 'public'
            UNION ALL SELECT 1 FROM pg_ts_template o JOIN pg_namespace n ON n.oid = o.tmplnamespace
                WHERE n.nspname = 'public'
            LIMIT 1
        """, stage="empty_registry")
        if cursor.fetchone() is not None:
            raise ValueError("Registry is populated; bootstrap refuses")
        _execute(cursor, """
            SELECT current_user = %s AND session_user = %s
                AND (r.rolsuper OR (r.rolcreaterole AND r.rolcreatedb))
                AND (r.rolsuper OR pg_has_role(current_user, d.datdba, 'USAGE'))
                AND (r.rolsuper OR pg_has_role(current_user, n.nspowner, 'USAGE'))
            FROM pg_roles r CROSS JOIN pg_database d CROSS JOIN pg_namespace n
            WHERE r.rolname = current_user AND d.datname = current_database()
                AND n.nspname = 'public'
        """, (root.username, root.username), stage="operator_authority")
        if cursor.fetchone() != (True,):
            raise ValueError("Bootstrap operator lacks dedicated registry ownership authority")
        _execute(cursor, sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 24"
        ).format(sql.Identifier(app.username), sql.Literal(app.password)), stage="create_application_role")
        # PG16+ CREATEROLE grants ADMIN, but neither SET nor INHERIT, back to
        # the creator. Add a separate grant whose grantor we can later revoke;
        # never grant postgres or cloudsqlsuperuser membership to the app.
        _execute(cursor, sql.SQL(
            "GRANT {} TO {} WITH ADMIN FALSE, INHERIT FALSE, SET TRUE GRANTED BY {}"
        ).format(sql.Identifier(app.username), sql.Identifier(root.username), sql.Identifier(root.username)),
            stage="temporary_owner_membership")
        # ALTER SCHEMA requires CREATE in this database for its new owner.
        # This ACL is temporary; NOCREATEDB remains a role attribute throughout.
        _execute(cursor, sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(app.database), sql.Identifier(app.username)), stage="temporary_schema_authority")
        _execute(cursor, "REVOKE CREATE ON SCHEMA public FROM PUBLIC", stage="restrict_public_schema")
        # public may be owned by pg_database_owner. Finish schema changes while
        # postgres is still the database owner, before changing that membership.
        _execute(cursor, sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
            sql.Identifier(app.username)), stage="transfer_schema")
        _execute(cursor, sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
            sql.Identifier(app.database), sql.Identifier(app.username)), stage="remove_temporary_schema_authority")
        _execute(cursor, sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(app.database)), stage="restrict_public_database")
        _execute(cursor, sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(app.database), sql.Identifier(app.username)), stage="application_database_access")
        _execute(cursor, sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
            sql.Identifier(app.database), sql.Identifier(app.username)), stage="transfer_database")
        _execute(cursor, sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
            sql.Identifier(app.username), sql.Identifier(root.username), sql.Identifier(root.username)),
            stage="remove_temporary_owner_membership")
    return {"database": app.database, "role": app.username,
            "superuser": False, "create_database": False, "create_role": False}


def main() -> int:
    try:
        result = bootstrap()
    except BootstrapDatabaseError as exc:
        print(json.dumps({"status": "refused", "category": "database", "stage": exc.stage,
                          "sqlstate": exc.sqlstate}))
        return 1
    except psycopg.Error as exc:
        state = exc.sqlstate if exc.sqlstate and re.fullmatch(r"[0-9A-Z]{5}", exc.sqlstate) else "unavailable"
        print(json.dumps({"status": "refused", "category": "database", "stage": "connection_or_commit",
                          "sqlstate": state}))
        return 1
    except Exception:  # noqa: BLE001 - database diagnostics must not disclose credentials.
        print("Dedicated registry bootstrap refused; inspect private operator configuration")
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
