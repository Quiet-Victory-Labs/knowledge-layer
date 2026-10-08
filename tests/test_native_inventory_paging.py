"""Native capture paging effects, with no vendor account or native ACL claim."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_layer.adapters.paging import NativeInventoryPager, continuation_request
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.connectors.external_inventory import make_request
from knowledge_layer.inventory import HARD_LIMITS, inventory_digest


def controls(**changes):
    now = datetime.now(UTC)
    values = {"provider": "dbt", "scope": "finance", "profile_digest": "a" * 64,
              "scan_id": "synthetic-scan", "page_index": 0, "cursor": None,
              "scan_started_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
              "scan_expires_at": (now + timedelta(seconds=299)).isoformat().replace("+00:00", "Z"),
              "inventory_limits": deepcopy(HARD_LIMITS)}
    values.update(changes)
    return make_request(**values)


def capture(count=101):
    return {"entities": [{"id": f"entity.{index}", "kind": "semantic_definition",
                          "payload": {"name": "Revenue", "native_id": str(index)},
                          "evidence_ids": [f"proof.{index}"]} for index in range(count)],
            "evidence": [{"id": f"proof.{index}", "kind": "upstream_semantic_definition",
                          "payload": {"native_record": {"unknown": None, "zero": 0,
                                                        "empty": "", "false": False, "text": "é收入"}}}
                         for index in range(count)],
            "watermark": {"kind": "content_sha256", "value": "b" * 64}}


def test_two_pages_capture_once_with_exact_native_values_and_idempotent_retry():
    pager, calls = NativeInventoryPager(), []
    source, request = capture(), controls()
    def load():
        calls.append(True)
        return source
    first = pager.page(request, operator_digest="c" * 64, load_records=load)
    assert first["listing_complete"] is False and len(first["entities"]) == 100
    first["entities"][0]["payload"]["name"] = "caller tampering"
    source["evidence"][0]["payload"]["native_record"]["zero"] = 99
    retry = pager.page(request, operator_digest="c" * 64, load_records=load)
    assert retry["entities"][0]["payload"]["name"] == "Revenue"
    second = pager.page(continuation_request(request, retry), operator_digest="c" * 64, load_records=load)
    assert second["listing_complete"] is True and second["next_cursor"] is None
    assert len(second["entities"]) == 1 and len(second["evidence"]) == 101
    assert second["evidence"][0]["payload"]["native_record"] == {
        "unknown": None, "zero": 0, "empty": "", "false": False, "text": "é收入"}
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("provider", "datahub"), ("scope", "studio"),
                                         ("scan_id", "new-scan"), ("profile_digest", "d" * 64)])
def test_continuation_cannot_cross_session_pins(field, value):
    pager, request = NativeInventoryPager(), controls()
    first = pager.page(request, operator_digest="c" * 64, load_records=capture)
    nxt = continuation_request(request, first)
    changed = {key: item for key, item in nxt.items() if key not in {
        "request_id", "protocol_version", "operation", "max_entities", "max_evidence", "max_response_bytes"}}
    changed[field] = value
    with pytest.raises(ConnectorError, match="unavailable"):
        pager.page(make_request(**changed), operator_digest="c" * 64, load_records=lambda: pytest.fail("No recapture"))


def test_rotated_operator_profile_or_process_restart_requires_new_scan():
    pager, request = NativeInventoryPager(), controls()
    first = pager.page(request, operator_digest="c" * 64, load_records=capture)
    nxt = continuation_request(request, first)
    for instance, pin in ((pager, "d" * 64), (NativeInventoryPager(), "c" * 64)):
        with pytest.raises(ConnectorError) as error:
            instance.page(nxt, operator_digest=pin, load_records=lambda: pytest.fail("No source IO"))
        assert error.value.code == "native_inventory_scan_lost"


def test_wrong_cursor_or_watermark_refuses_even_with_known_scan():
    pager, request = NativeInventoryPager(), controls()
    first = pager.page(request, operator_digest="c" * 64, load_records=capture)
    nxt = continuation_request(request, first)
    for cursor, watermark in (("forged", nxt["expected_watermark_digest"]), (nxt["cursor"], "0" * 64)):
        changed = {key: item for key, item in nxt.items() if key not in {
            "request_id", "protocol_version", "operation", "max_entities", "max_evidence", "max_response_bytes"}}
        changed.update(cursor=cursor, expected_watermark_digest=watermark)
        with pytest.raises(ConnectorError):
            pager.page(make_request(**changed), operator_digest="c" * 64, load_records=capture)


@pytest.mark.parametrize("fault", ["dangling", "duplicate_entity", "duplicate_proof", "oversized",
                                  "invalid_kind", "surrogate", "nan"])
def test_entire_capture_validated_before_any_page(fault):
    supplied, pager = capture(), NativeInventoryPager()
    if fault == "dangling":
        supplied["entities"][-1]["evidence_ids"] = ["missing"]
    elif fault == "duplicate_entity":
        supplied["entities"][-1] = deepcopy(supplied["entities"][0])
    elif fault == "duplicate_proof":
        supplied["evidence"][-1] = deepcopy(supplied["evidence"][0])
    elif fault == "oversized":
        supplied["evidence"][-1]["payload"]["text"] = "x" * 65536
    elif fault == "invalid_kind":
        supplied["entities"][-1]["kind"] = "executable_instruction"
    else:
        supplied["evidence"][-1]["payload"]["invalid"] = "\ud800" if fault == "surrogate" else float("nan")
    with pytest.raises((ConnectorError, UnicodeError)):
        pager.page(controls(), operator_digest="c" * 64, load_records=lambda: supplied)
    assert pager._active is None


@pytest.mark.parametrize("limit,value", [("max_pages", 1), ("max_entities", 100),
                                        ("max_evidence", 100), ("max_total_bytes", 1000)])
def test_scan_budget_refuses_initial_capture_without_partial_page(limit, value):
    budgets = deepcopy(HARD_LIMITS)
    budgets[limit] = value
    pager = NativeInventoryPager()
    with pytest.raises(ConnectorError) as error:
        pager.page(controls(inventory_limits=budgets), operator_digest="c" * 64, load_records=capture)
    assert error.value.code == "native_inventory_budget_exceeded"
    assert pager._active is None


def test_large_records_split_by_bytes_without_truncating():
    source = capture(6)
    for proof in source["evidence"]:
        proof["payload"]["native_record"]["expression"] = "x" * 60000
    pager, request = NativeInventoryPager(), controls()
    page = pager.page(request, operator_digest="c" * 64, load_records=lambda: source)
    evidence, total_pages = [], 0
    while True:
        total_pages += 1
        evidence += page["evidence"]
        if page["listing_complete"]:
            break
        request = continuation_request(request, page)
        page = pager.page(request, operator_digest="c" * 64, load_records=lambda: pytest.fail("No refetch"))
    assert total_pages == 2 and evidence == source["evidence"]


def test_deadline_refuses_before_source_io():
    request = controls(scan_started_at="2000-01-01T00:00:00Z", scan_expires_at="2000-01-01T00:05:00Z")
    with pytest.raises(ConnectorError) as error:
        NativeInventoryPager().page(request, operator_digest="c" * 64,
                                    load_records=lambda: pytest.fail("No expired source call"))
    assert error.value.code == "adapter_inventory_expired"


def test_new_failed_capture_cannot_reuse_previous_scope():
    pager, request = NativeInventoryPager(), controls()
    first = pager.page(request, operator_digest="c" * 64, load_records=capture)
    newer = deepcopy(request)
    newer["scan_id"] = "new"
    newer["request_id"] = inventory_digest({key: item for key, item in newer.items() if key != "request_id"})
    def denied():
        raise ConnectorError("source_denied", "Denied")
    with pytest.raises(ConnectorError):
        pager.page(newer, operator_digest="c" * 64, load_records=denied)
    with pytest.raises(ConnectorError) as error:
        pager.page(continuation_request(request, first), operator_digest="c" * 64, load_records=capture)
    assert error.value.code == "native_inventory_scan_lost"
