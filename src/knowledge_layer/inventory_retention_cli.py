"""Preview explicit aborted-inventory retention in the local PostgreSQL registry."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from knowledge_layer.inventory_retention import SCHEMA_VERSION, InventoryRetention
from knowledge_layer.migrate import SchemaMigrationError, ensure_current_schema
from knowledge_layer.operator_secrets import operator_secret
from knowledge_layer.store import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeNotFound,
    KnowledgeStore,
    KnowledgeValidationError,
)


def _failure(code: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "status": "refused", "error_code": code,
            "source_reads_executed": False, "source_writes_executed": False,
            "registry_writes_executed": None}


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print(json.dumps({**_failure("invalid_arguments"), "registry_writes_executed": False},
                         separators=(",", ":")))
        raise SystemExit(2)


def _reserve_output(path: Path) -> tuple[int, os.stat_result]:
    """Reserve the new private destination before any registry operation."""
    if not path.is_absolute():
        raise ValueError("Private output must be an absolute new file")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        return descriptor, os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        # Retain the private empty file; pathname replacement makes unlink unsafe.
        raise


def _write_reserved(descriptor: int, report: dict) -> None:
    with os.fdopen(descriptor, "wb", closefd=False) as handle:
        handle.write((json.dumps(report, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _same_output(path: Path, reserved: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return current.st_dev == reserved.st_dev and current.st_ino == reserved.st_ino


def _write_private(path: Path, report: dict) -> None:
    """Standalone private writer; main reserves its descriptor before applying."""
    descriptor, reserved = _reserve_output(path)
    try:
        _write_reserved(descriptor, report)
        if not _same_output(path, reserved):
            raise OSError("Private output path changed")
    finally:
        os.close(descriptor)


def _run(args) -> dict:
    url = operator_secret("DATABASE_URL")
    if not url:
        raise ValueError("Registry configuration is required")
    store = KnowledgeStore(database_url=url, initialize_schema=False)
    try:
        # Explicit operator upgrades remain separate; this command never migrates.
        ensure_current_schema(store)
        retention = InventoryRetention(store)
        options = {"created_before": args.created_before,
                   "max_runs": args.max_runs, "max_rows": args.max_rows}
        if args.apply:
            return retention.apply(args.tenant, args.connection,
                                   expected_preview_digest=args.expected_preview_digest, **options)
        return retention.preview(args.tenant, args.connection, **options)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Exact registry tenant ID")
    parser.add_argument("--connection", required=True, help="Exact registry source ID")
    parser.add_argument("--created-before", required=True,
                        help="Aware ISO creation cutoff, exclusive; no automatic retention age")
    parser.add_argument("--max-runs", type=int, default=20, help="Run deletion budget, 1–20")
    parser.add_argument("--max-rows", type=int, default=2000,
                        help="Total run/page/entity/evidence deletion rows, 1–10000")
    parser.add_argument("--apply", action="store_true", help="Apply only the exact previewed cohort")
    parser.add_argument("--expected-preview-digest", help="Required with --apply; exact 64-hex preview digest")
    parser.add_argument("--output", type=Path, help="Optional new absolute private receipt file, mode 0600")
    args = parser.parse_args(argv)
    if args.apply != (args.expected_preview_digest is not None):
        parser.error("Apply requires a preview digest")
    reservation = None
    if args.output is not None:
        try:
            reservation = _reserve_output(args.output)
        except (ValueError, OSError):
            report = {**_failure("private_output_refused"), "registry_writes_executed": False,
                      "receipt_file_written": False, "receipt_file_error": "reservation_failed"}
            print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
            return 2
    try:
        report = _run(args)
    except KnowledgeAccessDenied:
        report = _failure("offline_operator_required")
    except KnowledgeNotFound:
        report = _failure("source_unavailable")
    except KnowledgeValidationError:
        report = _failure("invalid_arguments")
    except KnowledgeConflict:
        report = _failure("retention_refused")
    except SchemaMigrationError:
        report = _failure("schema_unavailable")
    except (ValueError, OSError):
        report = _failure("configuration_or_output_refused")
    except Exception:  # noqa: BLE001 - never render credential-bearing database or driver failures
        report = _failure("registry_operation_failed")
    output_error = None
    if reservation is not None:
        descriptor, reserved = reservation
        written = False
        try:
            _write_reserved(descriptor, report)
            written = _same_output(args.output, reserved)
            if not written:
                output_error = "path_changed"
        except Exception:  # noqa: BLE001 - preserve actual mutation result, redact output exceptions
            output_error = "write_failed"
        finally:
            try:
                os.close(descriptor)
            except OSError:
                output_error = output_error or "close_failed"
        report = {**report, "receipt_file_written": written, "receipt_file_error": output_error}
        # Never unlink after a failure: this path could now identify an unrelated
        # replacement. The private partial file, if any, needs operator inspection.
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 2 if report["status"] == "refused" else 1 if output_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
