"""Offline operator commands for explicit source metadata grants."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from knowledge_layer.migrate import ensure_current_schema
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.store import KnowledgeError, KnowledgeStore, validate_tenant_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("grant", "revoke", "list"))
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--connection", required=True)
    parser.add_argument("--principal-type", choices=("user", "group"))
    parser.add_argument("--principal")
    parser.add_argument("--role", choices=("read", "manage"))
    parser.add_argument("--actor", default="operator")
    args = parser.parse_args()
    if args.action != "list" and not all((args.principal_type, args.principal)):
        parser.error("grant and revoke require --principal-type and --principal")
    if args.action == "grant" and not args.role:
        parser.error("grant requires --role")
    url = operator_secret("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL or DATABASE_URL_FILE is required")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        ensure_current_schema(store)
        tenant = validate_tenant_id(args.tenant)
        if args.action == "grant":
            store.grant_source_access(
                tenant, args.connection, principal_type=args.principal_type,
                principal_id=args.principal, role=args.role, granted_by=args.actor,
            )
            payload = {"action": "grant", "connection_id": args.connection, "result": "applied"}
        elif args.action == "revoke":
            store.revoke_source_access(
                tenant, args.connection, principal_type=args.principal_type,
                principal_id=args.principal, revoked_by=args.actor,
            )
            payload = {"action": "revoke", "connection_id": args.connection, "result": "applied"}
        else:
            if store.get_connection(tenant, args.connection) is None:
                raise KnowledgeError("Connection was not found")
            with store.engine.connect() as connection:
                rows = connection.execute(select(
                    store.source_grants.c.principal_type,
                    store.source_grants.c.principal_id,
                    store.source_grants.c.role,
                    store.source_grants.c.granted_at,
                    store.source_grants.c.granted_by,
                ).where(
                    store.source_grants.c.tenant_id == tenant,
                    store.source_grants.c.connection_id == args.connection,
                ).order_by(store.source_grants.c.principal_type, store.source_grants.c.principal_id)).mappings().all()
            payload = {"action": "list", "connection_id": args.connection,
                       "grants": [dict(row) for row in rows]}
    except (KnowledgeError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Source access operation refused: {exc}\n")
    except Exception:  # noqa: BLE001 - do not expose driver errors or database URLs.
        parser.exit(2, "Source access operation failed; inspect private database logs\n")
    finally:
        store.close()
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
