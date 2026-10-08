"""Check one explicit local adapter response file without source IO or credentials."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from knowledge_layer.adapters.conformance import (
    check_inventory_page,
    check_response,
    failure_receipt,
)
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import MAX_RESPONSE_BYTES, decode_response
from knowledge_layer.connectors.external_inventory import (
    INVENTORY_PROTOCOL,
    MAX_PAGE_BYTES,
    MAX_REQUEST_BYTES,
    validate_inventory_request,
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print(json.dumps(failure_receipt("invalid_arguments"), separators=(",", ":")))
        raise SystemExit(2)


def _read_input(path: Path, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """Open a bounded regular file once; reject symlinks, FIFOs and devices."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("input_not_regular")
        if info.st_size > max_bytes:
            raise ValueError("input_too_large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("input_too_large")
        return raw
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Explicit local UTF-8 JSON response file")
    parser.add_argument("--provider", required=True, help="Locally selected adapter provider identifier")
    parser.add_argument("--scope", required=True, help="Locally selected adapter scope identifier")
    parser.add_argument("--protocol-version", choices=("1.0", "1.1", "1.2", INVENTORY_PROTOCOL),
                        default="1.0")
    parser.add_argument("--inventory-request", type=Path,
                        help="Explicit local JSON request file required for protocol 1.3")
    parser.add_argument("--max-assets", type=int)
    parser.add_argument("--max-columns", type=int)
    parser.add_argument("--retain-semantic-samples", action="store_true")
    parser.add_argument("--asset-name", action="append", dest="asset_names",
                        help="Optional repeatable exact local asset selection")
    args = parser.parse_args(argv)
    paged = args.protocol_version == INVENTORY_PROTOCOL
    if paged:
        if (args.inventory_request is None or args.max_assets is not None
                or args.max_columns is not None or args.retain_semantic_samples
                or args.asset_names is not None):
            parser.error("Paged checks require a request and use its fixed page budgets")
    elif args.inventory_request is not None:
        parser.error("Only paged checks accept an inventory request")
    try:
        if paged:
            request_raw = _read_input(args.inventory_request, max_bytes=MAX_REQUEST_BYTES)
            request = validate_inventory_request(
                decode_response(request_raw, max_bytes=MAX_REQUEST_BYTES))
            if request["provider"] != args.provider or request["scope"] != args.scope:
                parser.error("The locally selected provider and scope must match the request")
        raw = _read_input(args.input, max_bytes=MAX_PAGE_BYTES if paged else MAX_RESPONSE_BYTES)
    except OSError:
        report = failure_receipt("input_unavailable")
    except ValueError as exc:
        report = failure_receipt(str(exc))
    except ConnectorError as exc:
        report = failure_receipt(exc.code)
    except Exception:  # noqa: BLE001 - never render private request parsing failures
        report = failure_receipt("conformance_internal_error")
    else:
        try:
            if paged:
                report = check_inventory_page(raw, request)
            else:
                report = check_response(
                    raw, provider=args.provider, scope=args.scope,
                    protocol_version=args.protocol_version,
                    max_assets=100 if args.max_assets is None else args.max_assets,
                    max_columns=5000 if args.max_columns is None else args.max_columns,
                    retain_semantic_samples=args.retain_semantic_samples, asset_names=args.asset_names,
                )
        except Exception:  # noqa: BLE001 - CLI redaction boundary never renders source exceptions
            report = failure_receipt("conformance_internal_error")
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
