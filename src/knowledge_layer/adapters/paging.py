"""Bound one captured native scope into the existing inventory wire protocol.

This is adapter-side paging of bounded observations, not a vendor cursor or an
atomic native snapshot. Restarting or replacing a scan requires a fresh capture.
"""

from __future__ import annotations

import secrets
from copy import deepcopy
from threading import Lock

from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_inventory import (
    assert_inventory_deadline,
    make_request,
    validate_inventory_page,
    validate_inventory_request,
)
from knowledge_layer.inventory import MAX_PAGE_BYTES, _encoded, _pins, _records, inventory_digest
from knowledge_layer.store import KnowledgeValidationError

MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_ENTITIES = 10_000
_OMIT = {"inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}
_SESSION = {"scan_id", "provider", "scope", "profile_digest", "scan_started_at",
            "scan_expires_at", "inventory_limits"}


def _refuse(code="native_inventory_invalid"):
    return ConnectorError(code, "The bounded native inventory capture is unavailable")


def _capture(value, request):
    """Validate all observations and their references before exposing any page."""
    try:
        if not isinstance(value, dict) or set(value) != {"entities", "evidence", "watermark"}:
            raise _refuse()
        entities, evidence = value["entities"], value["evidence"]
        if (not isinstance(entities, list) or not isinstance(evidence, list)
                or len(entities) > MAX_CAPTURE_ENTITIES or len(evidence) > 2 * MAX_CAPTURE_ENTITIES):
            raise _refuse("native_inventory_budget_exceeded")
        limits = request["inventory_limits"]
        if len(entities) > limits["max_entities"] or len(evidence) > limits["max_evidence"]:
            raise _refuse("native_inventory_budget_exceeded")
        entity_ids, evidence_ids, references = set(), set(), set()
        for items, is_evidence, ids in ((entities, False, entity_ids), (evidence, True, evidence_ids)):
            for item in items:
                _records([item], evidence=is_evidence)
                if item["id"] in ids:
                    raise _refuse()
                ids.add(item["id"])
                if not is_evidence:
                    references.update(item["evidence_ids"])
        if not references <= evidence_ids:
            raise _refuse()
        _pins({"namespace": "external_adapter", "selectors": [request["scope"]]},
              {"id": "external-adapter.inventory", "revision": request["profile_digest"]}, value["watermark"])
        if len(_encoded(value)) > MAX_CAPTURE_BYTES:
            raise _refuse("native_inventory_budget_exceeded")
        # Keep a 4 KiB wire/append-envelope allowance. Each whole record remains
        # intact; large expressions are never truncated to make a page fit.
        pages, current = [], {"entities": [], "evidence": []}
        for collection, maximum in (("entities", 100), ("evidence", 200)):
            for item in value[collection]:
                proposed = {key: list(records) for key, records in current.items()}
                proposed[collection].append(item)
                if len(proposed[collection]) > maximum or len(_encoded(proposed)) > MAX_PAGE_BYTES - 4096:
                    pages.append(current)
                    current = {"entities": [], "evidence": []}
                current[collection].append(item)
        pages.append(current)
        if len(pages) > limits["max_pages"]:
            raise _refuse("native_inventory_budget_exceeded")
        cursors = [None] + [secrets.token_hex(24) for _ in pages[1:]]
        total_bytes = 0
        for index, content in enumerate(pages):
            total_bytes += len(_encoded({"page_index": index, "cursor": cursors[index],
                "next_cursor": cursors[index + 1] if index + 1 < len(pages) else None,
                **content, **{key: "0" * 64 for key in ("scope_digest", "policy_digest", "watermark_digest")}}))
        if total_bytes > limits["max_total_bytes"]:
            raise _refuse("native_inventory_budget_exceeded")
        return deepcopy(pages), cursors, deepcopy(value["watermark"])
    except KnowledgeValidationError:
        raise _refuse() from None


class NativeInventoryPager:
    """One active, bounded capture per adapter process; no unbounded scan cache.

    Operator configuration is re-read by the caller on every request. Its digest
    includes credential material solely as a local invalidation pin; it is never
    returned, logged or confused with a native permission/version oracle.
    """

    def __init__(self):
        self._lock = Lock()
        self._active = None

    def page(self, request, *, operator_digest: str, load_records):
        request = validate_inventory_request(request)
        assert_inventory_deadline(request)
        if (not isinstance(operator_digest, str) or len(operator_digest) != 64
                or any(char not in "0123456789abcdef" for char in operator_digest)):
            raise _refuse()
        with self._lock:
            assert_inventory_deadline(request)
            active = self._active
            session = {key: request[key] for key in _SESSION}
            if active is not None and (active["operator_digest"] != operator_digest
                                       or active["session"] != session):
                self._active = active = None
            if active is None:
                if request["page_index"] != 0:
                    raise _refuse("native_inventory_scan_lost")
                # Clear an old capture before IO so denial cannot leave a usable
                # partial or previous response for this new session.
                self._active = None
                observations = load_records()
                assert_inventory_deadline(request)
                pages, cursors, watermark = _capture(observations, request)
                active = {"session": session, "operator_digest": operator_digest,
                          "pages": pages, "cursors": cursors, "watermark": watermark}
                self._active = active
            index = request["page_index"]
            if index >= len(active["pages"]) or request["cursor"] != active["cursors"][index]:
                raise _refuse("native_inventory_cursor_invalid")
            last = index + 1 == len(active["pages"])
            response = {key: value for key, value in request.items() if key not in _OMIT} | {
                "next_cursor": None if last else active["cursors"][index + 1],
                "listing_complete": last, "watermark": active["watermark"], **active["pages"][index]}
            assert_inventory_deadline(request)
            return validate_inventory_page(_encoded(response), request)


def continuation_request(first, page):
    """Build the next local request for a supplied page; never resume a vendor."""
    return make_request(**{key: first[key] for key in (
        "provider", "scope", "profile_digest", "scan_id", "scan_started_at", "scan_expires_at", "inventory_limits")},
        page_index=page["page_index"] + 1, cursor=page["next_cursor"],
        expected_watermark_digest=inventory_digest(page["watermark"]))
