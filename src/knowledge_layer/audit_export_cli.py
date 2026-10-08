"""Export governance events to a private, newline-delimited JSON file.

Usage: python -m knowledge_layer.audit_export_cli --tenant demo --output /private/audit.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from knowledge_layer.migrate import ensure_current_schema
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.store import KnowledgeError, KnowledgeStore


def export_events(
    store: KnowledgeStore, output: Path, *, tenant_id: str,
    connection_id: str | None = None,
) -> dict[str, object]:
    """Write a consistent, memory-bounded export without replacing a prior file."""
    if not output.is_absolute():
        raise ValueError("Audit output path must be absolute")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(output, flags, 0o600)
    count = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            for event in store.iter_audit_events(tenant_id, connection_id=connection_id):
                line = (json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
                handle.write(line)
                digest.update(line)
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"events": count, "sha256": digest.hexdigest(), "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--connection", help="Limit export to one source ID")
    parser.add_argument("--output", required=True, type=Path, help="New absolute JSONL path; existing files are refused")
    args = parser.parse_args()
    url = operator_secret("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL or DATABASE_URL_FILE is required")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        ensure_current_schema(store)
        result = export_events(store, args.output, tenant_id=args.tenant, connection_id=args.connection)
    except (KnowledgeError, ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"Audit export refused: {type(exc).__name__}\n")
    except Exception:  # noqa: BLE001 - do not expose driver errors or database URLs.
        parser.exit(2, "Audit export failed; inspect private database logs\n")
    finally:
        store.close()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
