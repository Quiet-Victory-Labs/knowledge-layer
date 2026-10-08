"""Strict paged discovery at the operator's existing read-only adapter boundary."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime

import httpx

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_adapter import SAFE_SCOPE, decode_response
from knowledge_layer.inventory import (
    MAX_ENTITIES_PER_PAGE,
    MAX_EVIDENCE_PER_PAGE,
    MAX_PAGE_BYTES,
    _encoded,
    _int,
    _limits,
    _object,
    _pins,
    _records,
    _text,
    inventory_digest,
)
from knowledge_layer.store import KnowledgeValidationError

INVENTORY_PROTOCOL = "1.3"
MAX_SCAN_SECONDS = 300
MAX_REQUEST_BYTES = 16 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_REQUEST_KEYS = {"protocol_version", "operation", "request_id", "scan_id", "provider", "scope",
                 "profile_digest", "page_index", "cursor", "scan_started_at", "scan_expires_at",
                 "expected_watermark_digest", "inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}
_ECHO_KEYS = _REQUEST_KEYS - {"inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}
_PAGE_KEYS = _ECHO_KEYS | {"next_cursor", "listing_complete", "watermark", "entities", "evidence"}


def _fail(code="adapter_inventory_invalid"):
    return ConnectorError(code, "The adapter inventory page or session is unavailable")


def _clock(value):
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        raise _fail()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _fail() from None
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise _fail()
    return parsed


def _cursor(value):
    if value is not None:
        _text(value, "opaque native cursor", 1024)


def validate_inventory_request(value: dict) -> dict:
    """Validate local request controls; this pure check makes no live-clock claim."""
    try:
        _object(value, _REQUEST_KEYS, "adapter request")
        if value["protocol_version"] != INVENTORY_PROTOCOL or value["operation"] != "inventory_page":
            raise _fail()
        for field in ("provider", "scope"):
            if not isinstance(value[field], str) or not SAFE_SCOPE.fullmatch(value[field]):
                raise _fail()
        for field in ("profile_digest", "request_id"):
            if not isinstance(value[field], str) or not _DIGEST.fullmatch(value[field]):
                raise _fail()
        _text(value["scan_id"], "scan ID", 64)
        _int(value["page_index"], "page index", 9999)
        _cursor(value["cursor"])
        limits = _limits(value["inventory_limits"])
        if value["page_index"] >= limits["max_pages"]:
            raise _fail("adapter_inventory_budget_exceeded")
        if (value["page_index"] == 0) != (value["cursor"] is None):
            raise _fail()
        expected = value["expected_watermark_digest"]
        if (value["page_index"] == 0 and expected is not None
                or value["page_index"] > 0 and (not isinstance(expected, str) or not _DIGEST.fullmatch(expected))):
            raise _fail()
        for field, maximum in (("max_entities", MAX_ENTITIES_PER_PAGE), ("max_evidence", MAX_EVIDENCE_PER_PAGE),
                               ("max_response_bytes", MAX_PAGE_BYTES)):
            if type(value[field]) is not int or value[field] != maximum:
                raise _fail()
        start, end = _clock(value["scan_started_at"]), _clock(value["scan_expires_at"])
        if not 0 < (end - start).total_seconds() <= MAX_SCAN_SECONDS:
            raise _fail()
        if len(_encoded(value)) > MAX_REQUEST_BYTES or inventory_digest(
                {key: item for key, item in value.items() if key != "request_id"}) != value["request_id"]:
            raise _fail()
        return deepcopy(value)
    except KnowledgeValidationError:
        raise _fail() from None


def make_request(*, provider, scope, profile_digest, scan_id, page_index, cursor,
                 scan_started_at, scan_expires_at, inventory_limits, expected_watermark_digest=None):
    value = {"protocol_version": INVENTORY_PROTOCOL, "operation": "inventory_page",
             "provider": provider, "scope": scope, "profile_digest": profile_digest, "scan_id": scan_id,
             "page_index": page_index, "cursor": cursor, "scan_started_at": scan_started_at,
             "scan_expires_at": scan_expires_at, "expected_watermark_digest": expected_watermark_digest,
             "inventory_limits": deepcopy(inventory_limits), "max_entities": MAX_ENTITIES_PER_PAGE,
             "max_evidence": MAX_EVIDENCE_PER_PAGE, "max_response_bytes": MAX_PAGE_BYTES}
    value["request_id"] = inventory_digest(value)
    return validate_inventory_request(value)


def assert_inventory_deadline(request, *, now=None):
    observed = now if now is not None else datetime.now(UTC)
    if (not isinstance(observed, datetime) or observed.tzinfo is None
            or not _clock(request["scan_started_at"]) <= observed < _clock(request["scan_expires_at"])):
        raise _fail("adapter_inventory_expired")


def validate_inventory_page(raw: bytes, expected_request: dict) -> dict:
    """Validate a complete page, never a full estate or native source permission."""
    request = validate_inventory_request(expected_request)
    page = decode_response(raw, max_bytes=MAX_PAGE_BYTES)
    try:
        _object(page, _PAGE_KEYS, "adapter page")
        if any(type(page[key]) is not type(request[key]) or page[key] != request[key] for key in _ECHO_KEYS):
            raise _fail("adapter_inventory_pin_changed")
        _cursor(page["next_cursor"])
        if type(page["listing_complete"]) is not bool or page["listing_complete"] != (page["next_cursor"] is None):
            raise _fail()
        if page["next_cursor"] is not None and page["next_cursor"] == page["cursor"]:
            raise _fail()
        watermark = _pins({"namespace": "external_adapter", "selectors": [request["scope"]]},
                          {"id": "external-adapter.inventory", "revision": request["profile_digest"]},
                          page["watermark"])["watermark"]
        if request["expected_watermark_digest"] is not None and inventory_digest(watermark) != request["expected_watermark_digest"]:
            raise _fail("adapter_inventory_pin_changed")
        entities, evidence = _records(page["entities"]), _records(page["evidence"], evidence=True)
        limits = request["inventory_limits"]
        if len(entities) > limits["max_entities"] or len(evidence) > limits["max_evidence"]:
            raise _fail("adapter_inventory_budget_exceeded")
        # The actual canonical append packet is bounded separately from wire bytes.
        packet_bytes = len(_encoded({"page_index": page["page_index"], "cursor": page["cursor"],
                         "next_cursor": page["next_cursor"], "entities": entities, "evidence": evidence,
                         **{key: "0" * 64 for key in ("scope_digest", "policy_digest", "watermark_digest")}}))
        if packet_bytes > MAX_PAGE_BYTES:
            raise _fail("adapter_response_too_large")
        if packet_bytes > limits["max_total_bytes"]:
            raise _fail("adapter_inventory_budget_exceeded")
        return deepcopy(page)
    except KnowledgeValidationError:
        raise _fail() from None


def fetch_inventory_page(profile: dict, request: dict) -> dict:
    """A cursor is POST body data; the operator target is never replaced/followed."""
    request = validate_inventory_request(request)
    assert_inventory_deadline(request)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if profile.get("bearer_token"):
        headers["Authorization"] = f"Bearer {profile['bearer_token']}"
    remaining = (_clock(request["scan_expires_at"]) - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise _fail("adapter_inventory_expired")
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(min(15, remaining), connect=min(5, remaining)), follow_redirects=False) as client,
            client.stream("POST", profile["endpoint"], headers=headers, json=request) as response,
        ):
            if response.status_code == 410:
                raise _fail("adapter_inventory_expired")
            if response.status_code != 200:
                raise _fail("adapter_unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise _fail("adapter_inventory_encoding_refused")
            chunks, size = [], 0
            for chunk in response.iter_raw():
                assert_inventory_deadline(request)
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    raise _fail("adapter_response_too_large")
                chunks.append(chunk)
        assert_inventory_deadline(request)
        return validate_inventory_page(b"".join(chunks), request)
    except httpx.HTTPError:
        raise _fail("adapter_unavailable") from None
