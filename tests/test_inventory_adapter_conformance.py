"""Independent offline page receipts and CLI boundaries, without source IO."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from knowledge_layer.adapters.conformance import check_inventory_page, failure_receipt
from knowledge_layer.connectors import external_adapter, external_inventory
from knowledge_layer.connectors.common import ConnectorError
from knowledge_layer.inventory import (
    HARD_LIMITS,
    MAX_PAGE_BYTES,
    MAX_RECORD_BYTES,
    inventory_digest,
)

PRIVATE = "private-supplied-page-canary"
PROFILE_DIGEST = hashlib.sha256(b"synthetic operator target identity").hexdigest()
ECHO_OMISSIONS = {"inventory_limits", "max_entities", "max_evidence", "max_response_bytes"}


def request(*, page_index=0, cursor=None, watermark=None):
    return external_inventory.make_request(
        provider="enterprise", scope="catalog", profile_digest=PROFILE_DIGEST,
        scan_id=PRIVATE, page_index=page_index, cursor=cursor,
        scan_started_at="2000-01-01T00:00:00Z", scan_expires_at="2000-01-01T00:05:00Z",
        inventory_limits=HARD_LIMITS,
        expected_watermark_digest=inventory_digest(watermark) if page_index else None,
    )


def page(controls, *, complete=False, watermark=None):
    return {key: deepcopy(value) for key, value in controls.items() if key not in ECHO_OMISSIONS} | {
        "next_cursor": None if complete else PRIVATE + "-continuation",
        "listing_complete": complete,
        "watermark": {"kind": "unknown", "value": None} if watermark is None else watermark,
        "entities": [{"id": PRIVATE + "-entity", "kind": "document",
                      "payload": {"excerpt": PRIVATE, "pointer": "documents/" + PRIVATE,
                                  "zero": 0, "unknown": None, "empty": "", "false": False},
                      "evidence_ids": [PRIVATE + "-evidence"]}],
        "evidence": [{"id": PRIVATE + "-evidence", "kind": "source_observation",
                      "payload": {"source_text": PRIVATE}}],
    }


def raw(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "scripts/check_adapter_contract.py"
    spec = importlib.util.spec_from_file_location("inventory_adapter_conformance_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files(tmp_path, *, controls=None, response=None):
    controls = request() if controls is None else controls
    response = page(controls) if response is None else response
    request_path, response_path = tmp_path / "request.json", tmp_path / "response.json"
    request_path.write_bytes(raw(controls))
    response_path.write_bytes(raw(response))
    return request_path, response_path


def arguments(request_path, response_path):
    return ["--input", str(response_path), "--provider", "enterprise", "--scope", "catalog",
            "--protocol-version", "1.3", "--inventory-request", str(request_path)]


@pytest.mark.parametrize("complete", [False, True])
def test_valid_page_never_certifies_complete_inventory_source_or_live_clock(monkeypatch, complete):
    def unavailable(*_args, **_kwargs):
        pytest.fail("An offline page check must not read credentials, use a clock or access a source")

    monkeypatch.setattr(external_adapter, "read_source_secret", unavailable)
    monkeypatch.setattr(external_inventory.httpx, "Client", unavailable)
    monkeypatch.setattr(external_inventory, "assert_inventory_deadline", unavailable)
    controls = request()
    supplied = raw(page(controls, complete=complete))
    report = check_inventory_page(supplied, controls)
    assert report["result"] == "passed" and report["protocol_version"] == "1.3"
    assert report["response_sha256"] == hashlib.sha256(supplied).hexdigest()
    assert report["request_sha256"] == controls["request_id"]
    assert report["counts_scope"] == "supplied_page"
    assert report["counts"] == {"entities": 1, "evidence": 1}
    assert report["listing"] == {
        "state": "declared_terminal_page" if complete else "continuing_page",
        "full_response_validated": True, "full_page_validated": True,
        "completeness_scope": "supplied_page", "inventory_complete_verified": False,
        "source_verified": False,
    }
    assert report["scan"] == {"clock_shape_validated": True, "clock_live_validated": False}
    assert report["watermark"] == {"state": "unknown", "continuation_pin_validated": False,
                                   "source_verified": False}
    assert report["qualification"] == {
        "wire_contract": True, "inventory_completion": False, "source_policy": False,
        "native_acl": False, "vendor_account": False, "business_meaning": False,
        "publication": False,
    }
    assert report["execution"] == {
        "network_calls": 0, "source_writes": 0, "registry_writes": 0, "model_calls": 0,
    }
    assert report["source_text_authority"] == "untrusted_evidence"
    encoded = json.dumps(report)
    for source_value in (PRIVATE, PROFILE_DIGEST, "documents/", "enterprise", "catalog"):
        assert source_value not in encoded
    assert len(encoded.encode("utf-8")) < 4096


@pytest.mark.parametrize("watermark", [{"kind": "unknown", "value": None},
                                      {"kind": "sequence", "value": 0},
                                      {"kind": "revision", "value": PRIVATE}])
def test_continuation_pins_watermark_and_keeps_values_out_of_receipt(watermark):
    controls = request(page_index=1, cursor=PRIVATE + "-previous", watermark=watermark)
    supplied = page(controls, watermark=watermark)
    report = check_inventory_page(raw(supplied), controls)
    assert report["result"] == "passed"
    assert report["watermark"] == {
        "state": "unknown" if watermark["kind"] == "unknown" else "declared",
        "continuation_pin_validated": True, "source_verified": False,
    }
    supplied["watermark"] = {"kind": "revision", "value": PRIVATE + "-changed"}
    assert check_inventory_page(raw(supplied), controls) == failure_receipt(
        "adapter_inventory_pin_changed")


def test_complete_empty_page_is_valid_but_not_a_scan_or_source_completeness_proof():
    controls = request()
    supplied = page(controls, complete=True) | {"entities": [], "evidence": []}
    report = check_inventory_page(raw(supplied), controls)
    assert report["result"] == "passed"
    assert report["counts"] == {"entities": 0, "evidence": 0}
    assert report["listing"]["inventory_complete_verified"] is False
    assert report["qualification"]["inventory_completion"] is False


@pytest.mark.parametrize("field,value", [
    ("provider", PRIVATE), ("scope", PRIVATE), ("profile_digest", "0" * 64),
    ("scan_id", PRIVATE + "-changed"), ("request_id", "0" * 64), ("page_index", False),
    ("cursor", PRIVATE), ("scan_started_at", "2000-01-01T00:00:01Z"),
    ("scan_expires_at", "2000-01-01T00:04:59Z"), ("expected_watermark_digest", "0" * 64),
])
def test_echo_pin_changes_refuse_content_free(field, value):
    controls = request()
    supplied = page(controls) | {field: value}
    report = check_inventory_page(raw(supplied), controls)
    assert report == failure_receipt("adapter_inventory_pin_changed")
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("case", ["unknown_member", "missing_member", "false_terminal",
    "true_continuation", "same_cursor", "entity_count", "evidence_count", "duplicate_entity",
    "duplicate_evidence", "unknown_kind", "record_bytes", "nonbool_complete", "unknown_watermark"])
def test_full_page_validation_prevents_selection_or_extra_payload_from_hiding_failure(case):
    watermark = {"kind": "unknown", "value": None}
    controls = request(page_index=1, cursor=PRIVATE + "-previous", watermark=watermark)
    supplied = page(controls, watermark=watermark)
    if case == "unknown_member":
        supplied[PRIVATE] = PRIVATE
    elif case == "missing_member":
        supplied.pop("evidence")
    elif case == "false_terminal":
        supplied["next_cursor"] = None
    elif case == "true_continuation":
        supplied["listing_complete"] = True
    elif case == "same_cursor":
        supplied["next_cursor"] = controls["cursor"]
    elif case in {"entity_count", "evidence_count"}:
        family = "entities" if case == "entity_count" else "evidence"
        maximum = 100 if family == "entities" else 200
        supplied[family] = [supplied[family][0] | {"id": f"record-{index}"}
                            for index in range(maximum + 1)]
    elif case in {"duplicate_entity", "duplicate_evidence"}:
        family = "entities" if case == "duplicate_entity" else "evidence"
        supplied[family].append(deepcopy(supplied[family][0]))
    elif case == "unknown_kind":
        supplied["entities"][0]["kind"] = PRIVATE
    elif case == "record_bytes":
        supplied["evidence"][0]["payload"] = {"text": "x" * MAX_RECORD_BYTES}
    elif case == "nonbool_complete":
        supplied["listing_complete"] = 0
    else:
        supplied["watermark"] = {"kind": "unknown", "value": 0}
    report = check_inventory_page(raw(supplied), controls)
    assert report == failure_receipt("adapter_inventory_invalid")
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize("supplied,code", [
    (b'{"protocol_version":"1.3","protocol_version":"1.3"}', "adapter_response_invalid"),
    (b'{"private":NaN}', "adapter_unavailable"),
    (b'{"private":1e999}', "adapter_unavailable"),
    (b'\xff', "adapter_unavailable"),
    (b'{"private":"\\ud800"}', "adapter_response_invalid"),
    (b'{"nested":{"\\udfff":"private"}}', "adapter_response_invalid"),
])
def test_shared_parser_refusals_are_identical_online_and_offline(monkeypatch, supplied, code):
    controls = request()
    now = datetime.now(UTC)
    controls["scan_started_at"] = now.isoformat().replace("+00:00", "Z")
    controls["scan_expires_at"] = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    controls["request_id"] = inventory_digest({key: value for key, value in controls.items() if key != "request_id"})
    observed_requests = []
    clocks = []

    def respond(outbound):
        observed_requests.append(json.loads(outbound.content))
        return httpx.Response(200, stream=httpx.ByteStream(supplied))

    client_type = httpx.Client
    monkeypatch.setattr(external_inventory.httpx, "Client", lambda **options: client_type(
        transport=httpx.MockTransport(respond), **options))
    monkeypatch.setattr(external_inventory, "assert_inventory_deadline", lambda value: clocks.append(value))
    with pytest.raises(ConnectorError) as online:
        external_inventory.fetch_inventory_page({"endpoint": "https://adapter.invalid/discover"}, controls)
    assert online.value.code == code
    assert check_inventory_page(supplied, controls) == failure_receipt(code)
    assert observed_requests == [controls] and clocks == [controls, controls, controls]


@pytest.mark.parametrize("escaped", [False, True])
def test_valid_non_bmp_exact_payload_and_missingness_are_preserved(escaped):
    controls = request()
    supplied = page(controls)
    supplied["entities"][0]["payload"]["exact"] = "  🧭𝄞  "
    encoded = json.dumps(supplied, ensure_ascii=escaped).encode("utf-8")
    validated = external_inventory.validate_inventory_page(encoded, controls)
    assert validated == supplied
    assert validated["entities"][0]["payload"] == {
        "excerpt": PRIVATE, "pointer": "documents/" + PRIVATE, "zero": 0,
        "unknown": None, "empty": "", "false": False, "exact": "  🧭𝄞  ",
    }
    assert check_inventory_page(encoded, controls)["result"] == "passed"


def test_raw_page_limit_accepts_exact_boundary_and_refuses_one_more_byte():
    controls = request()
    supplied = raw(page(controls))
    boundary = supplied + b" " * (MAX_PAGE_BYTES - len(supplied))
    report = check_inventory_page(boundary, controls)
    assert report["result"] == "passed" and report["response_bytes"] == MAX_PAGE_BYTES
    assert check_inventory_page(boundary + b" ", controls) == failure_receipt("adapter_response_too_large")


def test_canonical_append_budget_refuses_small_wire_representation_without_truncating():
    controls = request()
    supplied = page(controls) | {
        "entities": [{"id": f"entity-{index}", "kind": "asset", "evidence_ids": [],
                      "payload": {"numbers": [1e-9] * 9200}} for index in range(5)],
        "evidence": [],
    }
    encoded = raw(supplied).replace(b"1e-09", b"1e-9")
    assert len(encoded) < MAX_PAGE_BYTES
    assert all(len(raw(entity)) < MAX_RECORD_BYTES for entity in supplied["entities"])
    assert check_inventory_page(encoded, controls) == failure_receipt("adapter_response_too_large")


@pytest.mark.parametrize("field,value", [
    ("page_index", False), ("max_entities", 99), ("max_evidence", True),
    ("max_response_bytes", MAX_PAGE_BYTES + 1),
    ("scan_expires_at", "2000-01-01T00:05:01Z"),
    ("scan_started_at", "2000-01-01T00:00:00+00:00"),
    ("expected_watermark_digest", "0" * 64), ("cursor", PRIVATE),
])
def test_malformed_requests_refuse_without_page_or_source_work(monkeypatch, field, value):
    controls = request() | {field: value}
    controls["request_id"] = inventory_digest({key: item for key, item in controls.items()
                                               if key != "request_id"})

    def unavailable(*_args, **_kwargs):
        pytest.fail("Invalid requests must refuse before page parsing")

    monkeypatch.setattr(external_inventory, "decode_response", unavailable)
    assert check_inventory_page(b"private", controls) == failure_receipt("adapter_inventory_invalid")


def test_cli_valid_paged_check_is_content_free_and_does_not_write(cli, tmp_path, monkeypatch, capsys):
    request_path, response_path = files(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    def unavailable(*_args, **_kwargs):
        pytest.fail("Offline CLI must not access credentials or sources")

    monkeypatch.setattr(external_adapter, "read_source_secret", unavailable)
    monkeypatch.setattr(external_inventory.httpx, "Client", unavailable)
    assert cli.main(arguments(request_path, response_path)) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["result"] == "passed" and report["counts_scope"] == "supplied_page"
    assert PRIVATE not in captured.out + captured.err and captured.err == ""
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("extra", [[], ["--max-assets", "100"], ["--max-columns", "5000"],
                                 ["--asset-name", PRIVATE], ["--retain-semantic-samples"]])
def test_paged_arguments_require_request_and_reject_inapplicable_legacy_flags(
        cli, tmp_path, monkeypatch, capsys, extra):
    def unavailable(*_args, **_kwargs):
        pytest.fail("Incompatible flags must refuse before opening files")

    monkeypatch.setattr(cli, "_read_input", unavailable)
    args = ["--input", PRIVATE, "--provider", "enterprise", "--scope", "catalog",
            "--protocol-version", "1.3"]
    if extra:
        args += ["--inventory-request", str(tmp_path / PRIVATE)] + extra
    with pytest.raises(SystemExit) as failure:
        cli.main(args)
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == failure_receipt("invalid_arguments")
    assert PRIVATE not in captured.out + captured.err


def test_legacy_request_flag_is_refused_before_input_read(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_input", lambda *_args, **_kwargs: pytest.fail("No file reads"))
    with pytest.raises(SystemExit) as failure:
        cli.main(["--input", PRIVATE, "--provider", "enterprise", "--scope", "catalog",
                  "--protocol-version", "1.2", "--inventory-request", PRIVATE])
    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out) == failure_receipt("invalid_arguments")


@pytest.mark.parametrize("selector", ["provider", "scope"])
def test_cli_locally_selected_provider_and_scope_must_match_request(cli, tmp_path, capsys, selector):
    request_path, response_path = files(tmp_path)
    args = arguments(request_path, response_path)
    args[args.index("--" + selector) + 1] = PRIVATE
    with pytest.raises(SystemExit) as failure:
        cli.main(args)
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == failure_receipt("invalid_arguments")
    assert PRIVATE not in captured.out + captured.err


@pytest.mark.parametrize("target", ["request", "response"])
@pytest.mark.parametrize("mode,code", [("missing", "input_unavailable"),
    ("symlink", "input_unavailable"), ("directory", "input_not_regular"),
    ("fifo", "input_not_regular"), ("large", "input_too_large")])
def test_cli_both_files_are_bounded_regular_nonblocking_inputs(
        cli, tmp_path, capsys, target, mode, code):
    request_path, response_path = files(tmp_path)
    path = request_path if target == "request" else response_path
    path.unlink()
    if mode == "symlink":
        path.symlink_to(tmp_path / PRIVATE)
    elif mode == "directory":
        path.mkdir()
    elif mode == "fifo":
        os.mkfifo(path)
    elif mode == "large":
        limit = external_inventory.MAX_REQUEST_BYTES if target == "request" else MAX_PAGE_BYTES
        path.write_bytes(b" " * (limit + 1))
    assert cli.main(arguments(request_path, response_path)) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == failure_receipt(code)
    assert PRIVATE not in captured.out + captured.err


@pytest.mark.parametrize("supplied,code", [
    (b'{"scan_id":"private","scan_id":"duplicate"}', "adapter_response_invalid"),
    (b'{"private":NaN}', "adapter_unavailable"),
    (b'{"nested":{"\\udfff":"private"}}', "adapter_response_invalid"),
    (b'[]', "adapter_inventory_invalid"),
])
def test_cli_request_uses_shared_strict_json_parser(cli, tmp_path, capsys, supplied, code):
    request_path, response_path = files(tmp_path)
    request_path.write_bytes(supplied)
    assert cli.main(arguments(request_path, response_path)) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == failure_receipt(code)
    assert "private" not in captured.out + captured.err


@pytest.mark.parametrize("boundary", ["request", "page"])
def test_cli_internal_failure_is_redacted(cli, tmp_path, monkeypatch, capsys, boundary):
    request_path, response_path = files(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(cli, "validate_inventory_request" if boundary == "request"
                        else "check_inventory_page", unavailable)
    assert cli.main(arguments(request_path, response_path)) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == failure_receipt("conformance_internal_error")
    assert PRIVATE not in captured.out + captured.err
