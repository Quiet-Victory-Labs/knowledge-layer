"""Content-free offline receipts from the online adapter response normalizer."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from knowledge_layer.connectors import external_adapter, external_inventory
from knowledge_layer.connectors.common import ConnectorError

SCHEMA_VERSION = "adapter-conformance-v1"
ERROR_CODES = frozenset({
    "invalid_adapter_provider", "invalid_adapter_scope", "invalid_adapter_protocol",
    "invalid_scan_limit", "invalid_sample_policy", "invalid_asset_selection",
    "adapter_response_invalid", "adapter_response_too_large", "adapter_unavailable",
    "adapter_listing_incomplete", "asset_limit_exceeded", "column_limit_exceeded",
    "adapter_asset_not_found", "adapter_asset_name_ambiguous",
    "adapter_inventory_invalid", "adapter_inventory_budget_exceeded",
    "adapter_inventory_pin_changed", "adapter_inventory_expired",
})


def failure_receipt(code: str) -> dict[str, Any]:
    """Return only allowlisted codes; never render an exception or source value."""
    return {"schema_version": SCHEMA_VERSION, "result": "failed",
            "error_code": code if code in ERROR_CODES | {
                "input_unavailable", "input_not_regular", "input_too_large",
                "invalid_arguments", "conformance_internal_error",
            } else "conformance_internal_error"}


def check_inventory_page(raw: bytes, request: dict) -> dict[str, Any]:
    """Validate one supplied page without certifying a scan or a live source.

    The same pure request and page validators gate online discovery. A terminal
    page is a declaration by its producer; this offline check has no preceding
    pages, live clock, source grants or provider account to verify that claim.
    """
    try:
        controls = external_inventory.validate_inventory_request(request)
        page = external_inventory.validate_inventory_page(raw, controls)
    except ConnectorError as exc:
        return failure_receipt(exc.code)

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "passed",
        "protocol_version": external_inventory.INVENTORY_PROTOCOL,
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "response_bytes": len(raw),
        "request_sha256": controls["request_id"],
        "counts_scope": "supplied_page",
        "counts": {"entities": len(page["entities"]), "evidence": len(page["evidence"])},
        "listing": {
            "state": "declared_terminal_page" if page["listing_complete"] else "continuing_page",
            "full_response_validated": True,
            "full_page_validated": True,
            "completeness_scope": "supplied_page",
            "inventory_complete_verified": False,
            "source_verified": False,
        },
        "watermark": {
            "state": "unknown" if page["watermark"]["kind"] == "unknown" else "declared",
            "continuation_pin_validated": controls["page_index"] > 0,
            "source_verified": False,
        },
        "scan": {"clock_shape_validated": True, "clock_live_validated": False},
        "limits": {"response_bytes": controls["max_response_bytes"],
                   "entities": controls["max_entities"], "evidence": controls["max_evidence"]},
        "source_text_authority": "untrusted_evidence",
        "execution": {"network_calls": 0, "source_writes": 0, "registry_writes": 0,
                      "model_calls": 0},
        "qualification": {"wire_contract": True, "inventory_completion": False,
                          "source_policy": False, "native_acl": False, "vendor_account": False,
                          "business_meaning": False, "publication": False},
    }


def check_response(
    raw: bytes,
    *,
    provider: str,
    scope: str,
    protocol_version: str = external_adapter.PROTOCOL_VERSION,
    max_assets: int = external_adapter.MAX_ASSETS,
    max_columns: int = external_adapter.MAX_COLUMNS,
    retain_semantic_samples: bool = False,
    asset_names: list[str] | None = None,
) -> dict[str, Any]:
    """Check supplied bytes, not an endpoint, source policy or vendor account.

    All protocol validation and normalization is shared with online discovery.
    The receipt exposes aggregate counts and digests, never source names,
    pointers, definitions, document passages or normalized asset bodies.
    """
    try:
        result = external_adapter.normalize_response(
            raw, provider=provider, scope=scope, protocol_version=protocol_version,
            max_assets=max_assets, max_columns=max_columns,
            retain_semantic_samples=retain_semantic_samples, asset_names=asset_names,
        )
    except ConnectorError as exc:
        return failure_receipt(exc.code)

    assets = result["assets"]
    documents = [asset for asset in assets if asset["asset_type"] == "document"]
    coverage = {"documents": len(documents), "retention_enabled": retain_semantic_samples}
    for family in ("extraction", "retention"):
        profiles = [asset["profile"].get(f"document_{family}", {}) for asset in documents]
        coverage[f"{family}_complete"] = sum(profile.get("complete") is True for profile in profiles)
        coverage[f"{family}_truncated"] = sum(profile.get("truncated") is True for profile in profiles)
        coverage[f"{family}_unknown"] = sum(profile.get("complete") is None for profile in profiles)
        coverage[f"{family}_incomplete"] = sum(profile.get("complete") is False for profile in profiles)
    request = {"provider": provider, "scope": scope, "protocol_version": protocol_version,
               "max_assets": max_assets, "max_columns": max_columns,
               "retain_semantic_samples": retain_semantic_samples,
               "asset_names": external_adapter.validate_asset_names(asset_names)
               if asset_names is not None else None}
    request_bytes = json.dumps(request, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION, "result": "passed", "protocol_version": protocol_version,
        "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw),
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "counts_scope": "retained_projection",
        "counts": {
            "assets": len(assets), "columns": sum(len(asset["schema"]) for asset in assets),
            **{kind + "s": sum(asset["asset_type"] == kind for asset in assets)
               for kind in ("table", "document", "file")},
            "upstream_definitions": sum(asset["asset_type"] == "semantic_definition" for asset in assets),
            "retained_document_excerpts": sum(len(asset["profile"]["document_signals"].get(
                "candidate_descriptions", [])) for asset in documents),
        },
        "listing": {"state": "declared_complete" if result["source"]["listing_complete_declared"]
                    else "legacy_implicit", "full_response_validated": True,
                    "completeness_scope": "requested_asset_names" if asset_names is not None
                    else "supplied_response", "source_verified": False},
        "document_coverage": coverage,
        "limits": {"response_bytes": external_adapter.MAX_RESPONSE_BYTES,
                   "assets": max_assets, "columns": max_columns,
                   "document_terms_per_asset": external_adapter.MAX_DOCUMENT_TERMS},
        "execution": {"network_calls": 0, "source_writes": 0, "registry_writes": 0, "model_calls": 0},
        "qualification": {"wire_contract": True, "source_policy": False, "native_acl": False,
                          "vendor_account": False, "business_meaning": False, "publication": False},
    }
