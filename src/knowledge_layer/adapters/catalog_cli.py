"""Validate or map local catalog exports; no vendor IO or registry writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys

from knowledge_layer.adapters.catalog import (
    MAX_BYTES,
    SCHEMA_VERSION,
    inventory_records,
    validate_contract,
)
from knowledge_layer.adapters.catalog_mapping import binding_digest, map_export
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import decode_response, validate_profile
from knowledge_layer.inventory_refresh import inventory_profile_digest


def read_input(path, maximum=MAX_BYTES):
    """Read an explicitly selected regular artifact; detect changes during IO."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError
            raw = handle.read(maximum + 1)
            after = os.fstat(descriptor)
            if (len(raw) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError
            return raw
    except (OSError, ValueError):
        raise ConnectorError("catalog_input_unavailable", "The local catalog input is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def receipt(raw, records, *, export=None):
    return {"schema_version": "catalog-export-check-v1", "result": "passed",
            "export_schema": SCHEMA_VERSION, "input_sha256": hashlib.sha256(raw).hexdigest(),
            "export_sha256": hashlib.sha256(raw if export is None else export).hexdigest(),
            "counts": {"entities": len(records["entities"]), "evidence": len(records["evidence"])},
            "counts_scope": "supplied_export", "source_text_authority": "untrusted_evidence",
            "qualification": {"wire_contract": True, "vendor_account": False, "source_completeness": False,
                              "clock_live_validated": False,
                              "native_acl": False, "business_meaning": False, "publication": False},
            "execution": {"network_calls": 0, "registry_writes": 0, "source_writes": 0, "model_calls": 0}}


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        print(json.dumps({"result": "failed", "error_code": "invalid_arguments"}))
        raise SystemExit(2)


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "map", "pins"))
    parser.add_argument("--input", help="Explicit captured input; no URL fetching")
    parser.add_argument("--contract", required=True, help="Operator JSON with scope and export declarations")
    parser.add_argument("--mapping", help="Optional data-only JSON Pointer mapping")
    parser.add_argument("--profile", help="Platform profile used only to compute public pins")
    args = parser.parse_args(argv)
    if (args.operation != "pins" and (args.input is None or args.profile is not None)
            or args.operation == "map" and args.mapping is None
            or args.operation == "pins" and (args.input is not None or args.profile is None)):
        parser.error("")
    try:
        contract = decode_response(read_input(args.contract, 64 * 1024), max_bytes=64 * 1024)
        contract = validate_contract(contract)
        mapping = (decode_response(read_input(args.mapping, 16 * 1024), max_bytes=16 * 1024)
                   if args.mapping is not None else None)
        if args.operation == "pins":
            profile = decode_response(read_input(args.profile, 64 * 1024), max_bytes=64 * 1024)
            # Return digests only. Do not print the profile's credentials,
            # endpoints, account names, scope labels or native metadata.
            digest = binding_digest(contract["scope"], mapping)
            scope = contract["scope"]["scope_id"]
            validate_profile(profile, scope=scope)
            if (profile.get("protocol_version") != "1.3" or profile.get("provider") != contract["scope"]["provider"]
                    or scope not in profile.get("allowed_scopes", [])):
                raise ConnectorError("catalog_scope_binding_changed", "The profile binding is unavailable")
            copied = dict(profile)
            copied["inventory_scope_bindings"] = {**profile.get("inventory_scope_bindings", {}), scope: digest}
            validate_profile(copied, scope=scope)
            print(json.dumps({"result": "passed", "scope_binding_digest": digest,
                              "profile_digest": inventory_profile_digest(copied),
                              "digest_basis": "supplied_profile_with_returned_scope_binding"}))
            return 0
        raw = read_input(args.input)
        encoded = map_export(raw, contract=contract, mapping=mapping) if mapping is not None else raw
        records = inventory_records(encoded, expected_scope=contract["scope"])
        package = decode_response(encoded, max_bytes=MAX_BYTES)
        if any(package[key] != contract[key] for key in contract if key != "scope"):
            raise ConnectorError("catalog_contract_mismatch", "The declared catalog capture contract differs")
        if args.operation == "map":
            sys.stdout.write(encoded.decode("utf-8") + "\n")
        else:
            print(json.dumps(receipt(raw, records, export=encoded), separators=(",", ":")))
        return 0
    except ConnectorError as exc:
        print(json.dumps({"result": "failed", "error_code": exc.code}))
        return 1
    except Exception:  # noqa: BLE001 - sanitized local input boundary, never native/profile diagnostics
        print(json.dumps({"result": "failed", "error_code": "catalog_export_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
