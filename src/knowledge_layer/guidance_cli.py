"""Offline operator review/publication and portable company guidance export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_layer.migrate import ensure_current_schema
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.store import (
    KnowledgeError,
    KnowledgeNotFound,
    KnowledgeStore,
    validate_tenant_id,
)


def main() -> int:
    from knowledge_layer.guidance import GuidanceRegistry
    from knowledge_layer.guidance_export import export_guidance

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("draft", "get", "publish", "withdraw", "resolve", "export"):
        command = sub.add_parser(action)
        command.add_argument("--tenant", default="demo")
        if action in {"draft", "get", "publish", "withdraw"}:
            command.add_argument("--slug", required=True)
        if action == "draft":
            command.add_argument("--file", type=Path, required=True)
            command.add_argument("--expected-revision", type=int, default=0)
        if action in {"get", "publish", "withdraw"}:
            command.add_argument("--revision", type=int, required=action != "get")
        if action == "publish":
            command.add_argument("--expected-digest", required=True)
            command.add_argument("--rationale", required=True)
        if action == "withdraw":
            command.add_argument("--reason", required=True)
        if action in {"resolve", "export"}:
            command.add_argument("--business-unit")
            command.add_argument("--task")
            command.add_argument("--connection", action="append", dest="connection_ids")
        if action == "export":
            command.add_argument("--client", default="portable")
    args = parser.parse_args()
    url = operator_secret("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL or DATABASE_URL_FILE is required")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        ensure_current_schema(store)
        registry = GuidanceRegistry(store)
        tenant = validate_tenant_id(args.tenant)
        if args.action == "draft":
            if args.file.stat().st_size > 65536:
                raise ValueError("Guidance input exceeds the 64 KiB file budget")
            payload = registry.draft(tenant, args.slug,
                                     json.loads(args.file.read_text(encoding="utf-8")),
                                     expected_revision=args.expected_revision)
        elif args.action == "get":
            payload = registry.get(tenant, args.slug, revision=args.revision)
            if payload is None:
                raise KnowledgeNotFound("Guidance was not found")
        elif args.action == "publish":
            payload = registry.publish(tenant, args.slug, revision=args.revision,
                                       expected_digest=args.expected_digest,
                                       rationale=args.rationale)
        elif args.action == "withdraw":
            payload = registry.revoke(tenant, args.slug, revision=args.revision, reason=args.reason)
        else:
            payload = registry.resolve(tenant, business_unit=args.business_unit, task=args.task,
                                       connection_ids=args.connection_ids)
            if args.action == "export":
                payload = export_guidance(payload, client=args.client)
    except (KnowledgeError, ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Company guidance operation refused: {exc}\n")
    except Exception:  # noqa: BLE001 - driver diagnostics may contain secrets.
        parser.exit(2, "Company guidance operation failed; inspect private registry logs\n")
    finally:
        store.close()
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
