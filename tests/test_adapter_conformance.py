"""Independent response/CLI conformance checks, with no network or source fixture."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from adapter_response_fixture import streamed_response

from knowledge_layer.adapters.conformance import check_response, failure_receipt
from knowledge_layer.connectors import external_adapter
from knowledge_layer.connectors.common import ConnectorError

REQUEST = {"provider": "enterprise", "scope": "catalog", "protocol_version": "1.2"}
PRIVATE_TEXT = "source-body-canary-private"
PROFILE_REFERENCE = "KNOWLEDGE_SOURCE_CONFORMANCE"  # Public mocked operator reference, no credential.


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, identity"])
def test_legacy_adapter_refuses_compression_before_reading_or_decoding_body(monkeypatch, encoding):
    monkeypatch.setenv(PROFILE_REFERENCE, json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["catalog"], "protocol_version": "1.2",
    }))
    class UnreadableBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Compressed body must be refused before any decoder or body read")
            yield b""  # pragma: no cover

    real_client = httpx.Client
    def handler(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=UnreadableBody())

    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ConnectorError) as refused:
        external_adapter.discover({"secret_ref": PROFILE_REFERENCE, "scope": "catalog"})
    assert refused.value.code == "adapter_encoding_refused"


def _table():
    return {"name": "Readings", "pointer": "tables/readings", "revision": "vendor-7",
            "schema": [{"name": "sensor_id", "native_type": "BIGINT", "nullable": False, "field_id": 1},
                       {"name": "temperature", "native_type": "NUMERIC(12,3)", "nullable": None, "field_id": 2}],
            "observations": {"row_count": 0, "row_count_basis": "snapshot_summary",
                             "primary_key": ["sensor_id"], "foreign_keys": [], "lineage": []}}


def _document():
    return {"name": "Policy", "pointer": "documents/policy.md", "asset_type": "document",
            "schema": [], "revision": "policy-1",
            "document_coverage": {"extraction_complete": True, "extraction_truncated": False,
                                  "retention_complete": True, "retention_truncated": False},
            "document_terms": [{"term": "Retention", "excerpt": "  " + PRIVATE_TEXT + "  ",
                                "source_locator": {"format": "md", "line_start": 3, "line_end": 3}}]}


def _definition():
    return {"name": "Temperature", "pointer": "definitions/temperature", "asset_type": "semantic_definition",
            "schema": [], "upstream_definition": {"id": "term-17", "kind": "ontology_term",
                "namespace": "research", "definition": PRIVATE_TEXT, "version": None,
                "source_observed_at": None, "owner": None, "unit": "Celsius", "formula": None,
                "review_status": "approved", "reference": "definitions/temperature"}}


def _payload():
    return {"protocol_version": "1.2", "listing_complete": True, "next_cursor": None,
            "assets": [_table(), _document(), _definition()]}


def _raw(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _check(payload, **options):
    return check_response(_raw(payload), **(REQUEST | options))


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "scripts/check_adapter_contract.py"
    spec = importlib.util.spec_from_file_location("adapter_conformance_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rich_receipt_is_bounded_content_free_and_not_a_qualification(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Offline conformance must not access credentials or network")

    monkeypatch.setattr(external_adapter, "read_source_secret", unexpected)
    monkeypatch.setattr(external_adapter.httpx, "Client", unexpected)
    raw = _raw(_payload())
    report = check_response(raw, **REQUEST, retain_semantic_samples=True)
    assert report["result"] == "passed"
    assert report["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["counts"] == {"assets": 3, "columns": 2, "tables": 1, "documents": 1,
                                "files": 0, "upstream_definitions": 1, "retained_document_excerpts": 1}
    assert report["document_coverage"] == {
        "documents": 1, "retention_enabled": True,
        "extraction_complete": 1, "extraction_truncated": 0, "extraction_unknown": 0,
        "extraction_incomplete": 0, "retention_complete": 1, "retention_truncated": 0,
        "retention_unknown": 0, "retention_incomplete": 0,
    }
    assert report["execution"] == {"network_calls": 0, "source_writes": 0, "registry_writes": 0, "model_calls": 0}
    assert report["qualification"] == {"wire_contract": True, "source_policy": False, "native_acl": False,
        "vendor_account": False, "business_meaning": False, "publication": False}
    encoded = json.dumps(report)
    assert PRIVATE_TEXT not in encoded and "tables/readings" not in encoded and "Temperature" not in encoded
    assert len(encoded.encode("utf-8")) < 4096


def test_normalizer_preserves_source_unknown_zero_and_exact_text_without_approval():
    normalized = external_adapter.normalize_response(_raw(_payload()), **REQUEST, retain_semantic_samples=True)
    table = next(asset for asset in normalized["assets"] if asset["asset_type"] == "table")
    assert table["schema"][1] == {"name": "temperature", "native_type": "NUMERIC(12,3)",
                                   "data_type": "number", "nullable": None, "field_id": 2}
    assert table["profile"]["row_count"] == 0 and table["profile"]["row_count_is_complete"] is False
    native = next(asset for asset in normalized["assets"] if asset["asset_type"] == "semantic_definition")
    assert native["upstream_definition"]["version"] is None
    assert native["upstream_definition"]["source_observed_at"] is None
    assert native["upstream_definition"]["definition"] == PRIVATE_TEXT
    assert native["upstream_definition"]["review_status"] == "approved" and native["trust_state"] == "unverified"
    doc = next(asset for asset in normalized["assets"] if asset["asset_type"] == "document")
    assert doc["profile"]["document_signals"]["candidate_descriptions"][0]["excerpt"] == "  " + PRIVATE_TEXT + "  "
    assert normalized["capabilities"]["query_execution"] is False


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_online_discovery_and_offline_normalization_are_identical(monkeypatch, version):
    payload = _payload()
    payload["protocol_version"] = version
    if version != "1.2":
        payload["assets"].pop()
    if version == "1.0":
        payload.pop("listing_complete")
    raw = _raw(payload)
    monkeypatch.setenv("KNOWLEDGE_SOURCE_CONFORMANCE", json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["catalog"], "protocol_version": version,
    }))
    real_client = httpx.Client
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return streamed_response(200, content=raw)

    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    settings = {"max_assets": 3, "max_columns": 2, "retain_semantic_samples": True,
                "asset_names": ["Readings"], "source_id": "source-exact-17", "name": "Bounded catalog"}
    online = external_adapter.discover({"secret_ref": PROFILE_REFERENCE, "scope": "catalog", **settings})
    offline = external_adapter.normalize_response(raw, **(REQUEST | {"protocol_version": version}), **settings)
    assert online == offline
    assert online["source"]["id"] == "source-exact-17"
    assert online["source"]["listing_complete_declared"] is (version != "1.0")
    receipt = check_response(raw, **(REQUEST | {"protocol_version": version}), **{
        key: value for key, value in settings.items() if key not in {"source_id", "name"}
    })
    assert receipt["listing"]["state"] == ("legacy_implicit" if version == "1.0" else "declared_complete")
    assert calls == [{"protocol_version": version, "scope": "catalog", "max_assets": 3,
                      "max_columns": 2, "include_document_excerpts": True}]
    assert "asset_names" not in calls[0]


def test_retention_off_drops_passages_and_does_not_assert_retention_complete():
    payload = _payload()
    report = _check(payload)
    assert report["counts"]["retained_document_excerpts"] == 0
    assert report["document_coverage"]["retention_enabled"] is False
    assert report["document_coverage"]["retention_complete"] == 0
    assert report["document_coverage"]["retention_unknown"] == 1
    original = external_adapter.normalize_response(_raw(payload), **REQUEST)
    payload["assets"][1]["document_terms"][0]["excerpt"] = "A different valid private passage."
    changed = external_adapter.normalize_response(_raw(payload), **REQUEST)
    assert original == changed
    document = next(asset for asset in original["assets"] if asset["asset_type"] == "document")
    assert PRIVATE_TEXT not in json.dumps(document)
    assert document["profile"]["content_retained"] is False


def test_partial_document_extraction_is_valid_wire_but_never_full_knowledge():
    payload = _payload()
    payload["assets"][1]["document_coverage"] = {
        "extraction_complete": False, "extraction_truncated": True,
        "retention_complete": False, "retention_truncated": True,
    }
    report = _check(payload, retain_semantic_samples=True)
    assert report["result"] == "passed"
    assert report["document_coverage"]["extraction_incomplete"] == 1
    assert report["document_coverage"]["retention_truncated"] == 1
    assert report["qualification"]["publication"] is False


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_complete_empty_is_explicitly_distinct_from_failed_listing(version):
    report = _check({"protocol_version": version, "listing_complete": True, "assets": []}, protocol_version=version)
    assert report["result"] == "passed" and report["counts"]["assets"] == 0
    assert report["listing"]["state"] == "declared_complete" and report["listing"]["source_verified"] is False


@pytest.mark.parametrize("assets", [[], [_table()]])
def test_legacy_missing_completeness_declaration_remains_implicit(assets):
    payload = {"protocol_version": "1.0", "assets": assets}
    implicit = _check(payload, protocol_version="1.0")
    explicit = _check(payload | {"listing_complete": True}, protocol_version="1.0")
    assert implicit["result"] == "passed"
    assert implicit["counts"] == explicit["counts"]
    assert implicit["listing"] == explicit["listing"] | {"state": "legacy_implicit"}
    assert implicit["listing"]["full_response_validated"] is True
    assert implicit["listing"]["source_verified"] is False
    normalized = external_adapter.normalize_response(_raw(payload), **(REQUEST | {"protocol_version": "1.0"}))
    assert normalized["source"]["listing_complete_declared"] is False


@pytest.mark.parametrize("change", [
    {"listing_complete": False}, {"listing_complete": None}, {"listing_complete": 1}, {"next_cursor": "private-cursor"},
])
def test_incomplete_listing_cannot_pass(change):
    assert _check(_payload() | change) == failure_receipt("adapter_listing_incomplete")


def test_rich_versions_require_explicit_complete_flag_and_reject_version_downgrade():
    payload = _payload()
    payload.pop("listing_complete")
    assert _check(payload)["error_code"] == "adapter_listing_incomplete"
    assert _check(_payload(), protocol_version="1.1")["error_code"] == "adapter_response_invalid"
    assert _check(_payload(), protocol_version="3.0")["error_code"] == "invalid_adapter_protocol"


@pytest.mark.parametrize("options,code", [
    ({"provider": "private provider"}, "invalid_adapter_provider"),
    ({"scope": "private scope"}, "invalid_adapter_scope"),
    ({"protocol_version": True}, "invalid_adapter_protocol"),
    ({"max_assets": True}, "invalid_scan_limit"),
    ({"max_assets": 101}, "invalid_scan_limit"),
    ({"max_columns": 0}, "invalid_scan_limit"),
    ({"retain_semantic_samples": 1}, "invalid_sample_policy"),
    ({"asset_names": []}, "invalid_asset_selection"),
    ({"asset_names": ["Readings", "Readings"]}, "invalid_asset_selection"),
])
def test_request_scope_and_budgets_are_strict(options, code):
    assert _check(_payload(), **options) == failure_receipt(code)


@pytest.mark.parametrize("raw,code", [
    (b'{"protocol_version":"1.2","listing_complete":false,"listing_complete":true,"assets":[]}', "adapter_response_invalid"),
    (b'{"protocol_version":"1.2","listing_complete":true,"assets":[{"name":"A","name":"B","pointer":"a"}]}', "adapter_response_invalid"),
    (b'{"protocol_version":"1.2","listing_complete":true,"assets":[],"extra":NaN}', "adapter_unavailable"),
    (b'{"protocol_version":"1.2","listing_complete":true,"assets":[],"extra":Infinity}', "adapter_unavailable"),
    (b'{"protocol_version":"1.2","listing_complete":true,"assets":[],"extra":1e9999}', "adapter_unavailable"),
    (b'{"private":"\xff"}', "adapter_unavailable"),
    (json.dumps({"protocol_version": "1.2", "listing_complete": True, "assets": []}).encode("utf-16"), "adapter_unavailable"),
    (b"[" * 20000 + b"0" + b"]" * 20000, "adapter_response_invalid"),
], ids=["duplicate-root-key", "duplicate-nested-key", "nan", "infinity", "overflow", "invalid-utf8", "utf16", "deep-json"])
def test_unique_key_finite_utf8_json_boundary(raw, code):
    assert check_response(raw, **REQUEST) == failure_receipt(code)


@pytest.mark.parametrize("raw", [
    b'{"protocol_version":"1.2","listing_complete":false,"listing_complete":true,"assets":[]}',
    b'{"protocol_version":"1.2","listing_complete":true,"assets":[],"extra":1e9999}',
    json.dumps({"protocol_version": "1.2", "listing_complete": True, "assets": []}).encode("utf-16"),
], ids=["duplicate-keys", "nonfinite-number", "non-utf8"])
def test_online_and_offline_share_strict_parser_refusals(monkeypatch, raw):
    monkeypatch.setenv("KNOWLEDGE_SOURCE_CONFORMANCE", json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["catalog"], "protocol_version": "1.2",
    }))
    real_client = httpx.Client
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, content=raw)), **kwargs))
    with pytest.raises(ConnectorError) as failure:
        external_adapter.discover({"secret_ref": PROFILE_REFERENCE, "scope": "catalog"})
    assert check_response(raw, **REQUEST) == failure_receipt(failure.value.code)


@pytest.mark.parametrize("codepoint", [0xD800, 0xDFFF], ids=["lone-high", "lone-low"])
@pytest.mark.parametrize("location", [
    "asset-name", "column-name", "native-type", "definition", "excerpt", "nested-extra", "nested-key", "root-key",
])
def test_online_and_offline_refuse_escaped_lone_surrogates_everywhere(monkeypatch, codepoint, location):
    payload = _payload()
    value = PRIVATE_TEXT + chr(codepoint)
    if location == "asset-name":
        payload["assets"][0]["name"] = value
    elif location == "column-name":
        payload["assets"][0]["schema"][0]["name"] = value
    elif location == "native-type":
        payload["assets"][0]["schema"][0]["native_type"] = value
    elif location == "definition":
        payload["assets"][2]["upstream_definition"]["definition"] = value
    elif location == "excerpt":
        payload["assets"][1]["document_terms"][0]["excerpt"] = value
    elif location == "nested-extra":
        payload["extra"] = [{"ignored": [value]}]
    elif location == "nested-key":
        payload["extra"] = [{value: []}]
    else:
        payload[value] = []
    raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_CONFORMANCE", json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["catalog"], "protocol_version": "1.2",
    }))
    real_client = httpx.Client
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, content=raw)), **kwargs))
    for retain in (False, True):
        with pytest.raises(ConnectorError) as failure:
            external_adapter.discover({"secret_ref": PROFILE_REFERENCE, "scope": "catalog",
                                       "retain_semantic_samples": retain})
        assert failure.value.code == "adapter_response_invalid"
        assert PRIVATE_TEXT not in str(failure.value)
        receipt = check_response(raw, **REQUEST, retain_semantic_samples=retain)
        assert receipt == failure_receipt("adapter_response_invalid")
        assert PRIVATE_TEXT not in json.dumps(receipt)


@pytest.mark.parametrize("escaped", [False, True], ids=["literal-utf8", "paired-json-escape"])
def test_valid_non_bmp_metadata_preserves_exact_online_and_offline_text(monkeypatch, escaped):
    payload = _payload()
    native_name = "  Readings 🚀 Ω  "
    excerpt = "  café 🚀 policy\nsecond line  "
    definition = " Research 🚀\nexact meaning "
    payload["assets"][0]["name"] = native_name
    payload["assets"][1]["document_terms"][0]["excerpt"] = excerpt
    payload["assets"][2]["upstream_definition"]["definition"] = definition
    payload["extra"] = [{"nested 🚀": ["valid 🚀"]}]
    raw = json.dumps(payload, ensure_ascii=escaped).encode("utf-8")
    monkeypatch.setenv("KNOWLEDGE_SOURCE_CONFORMANCE", json.dumps({
        "endpoint": "https://adapter.example/discover", "provider": "enterprise",
        "allowed_scopes": ["catalog"], "protocol_version": "1.2",
    }))
    real_client = httpx.Client
    monkeypatch.setattr(external_adapter.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _request: streamed_response(200, content=raw)), **kwargs))
    normalized = external_adapter.normalize_response(raw, **REQUEST, retain_semantic_samples=True)
    online = external_adapter.discover({"secret_ref": PROFILE_REFERENCE, "scope": "catalog",
                                       "retain_semantic_samples": True})
    assert online == normalized
    table = next(asset for asset in normalized["assets"] if asset["asset_type"] == "table")
    document = next(asset for asset in normalized["assets"] if asset["asset_type"] == "document")
    native = next(asset for asset in normalized["assets"] if asset["asset_type"] == "semantic_definition")
    assert table["name"] == native_name
    assert document["profile"]["document_signals"]["candidate_descriptions"][0]["excerpt"] == excerpt
    assert native["upstream_definition"]["definition"] == definition
    json.dumps(normalized, ensure_ascii=False).encode("utf-8")
    receipt = check_response(raw, **REQUEST, retain_semantic_samples=True)
    assert receipt["result"] == "passed" and receipt["qualification"]["business_meaning"] is False
    assert native_name not in json.dumps(receipt, ensure_ascii=False)


def test_response_byte_limit_counts_actual_utf8_bytes():
    payload = {"protocol_version": "1.2", "listing_complete": True, "assets": [], "extra": "界" * 700000}
    raw = _raw(payload)
    assert len(raw) > external_adapter.MAX_RESPONSE_BYTES
    assert len(raw.decode("utf-8")) < external_adapter.MAX_RESPONSE_BYTES
    assert check_response(raw, **REQUEST) == failure_receipt("adapter_response_too_large")


def test_maximum_response_bytes_is_accepted_without_clipping():
    raw = _raw({"protocol_version": "1.2", "listing_complete": True, "assets": []})
    raw += b" " * (external_adapter.MAX_RESPONSE_BYTES - len(raw))
    report = check_response(raw, **REQUEST)
    assert report["result"] == "passed" and report["response_bytes"] == external_adapter.MAX_RESPONSE_BYTES


@pytest.mark.parametrize("mode,code", [
    ("assets", "asset_limit_exceeded"), ("columns", "column_limit_exceeded"),
    ("unselected-invalid", "adapter_response_invalid"),
    ("missing", "adapter_asset_not_found"), ("ambiguous", "adapter_asset_name_ambiguous"),
])
def test_selection_cannot_hide_full_response_failures(mode, code):
    payload = _payload()
    settings = {"asset_names": ["Readings"]}
    if mode == "assets":
        settings["max_assets"] = 1
    elif mode == "columns":
        settings["max_columns"] = 1
    elif mode == "unselected-invalid":
        payload["assets"][1]["pointer"] = "https://signed.example/" + PRIVATE_TEXT
    elif mode == "missing":
        settings["asset_names"] = [PRIVATE_TEXT]
    else:
        duplicate = deepcopy(payload["assets"][0])
        duplicate["pointer"] = "tables/duplicate"
        payload["assets"].append(duplicate)
    assert _check(payload, **settings) == failure_receipt(code)


def test_selection_reports_projection_counts_and_order_independent_request_pin():
    one = _check(_payload(), asset_names=["Policy", "Readings"])
    two = _check(_payload(), asset_names=["Readings", "Policy"])
    assert one == two
    assert one["counts"]["assets"] == 2 and one["counts"]["upstream_definitions"] == 0
    assert one["listing"]["full_response_validated"] is True
    assert one["listing"]["completeness_scope"] == "requested_asset_names"
    assert one["request_sha256"] != _check(_payload())["request_sha256"]


@pytest.mark.parametrize("mode", [
    "columns", "pointers", "field-id", "nullable", "native-type", "foreign-key", "count", "clock",
    "definition-identity", "definition-kind", "definition-reference", "definition-schema",
    "document-coverage", "missing-coverage", "document-locator", "document-excerpt",
])
def test_schema_provenance_and_retained_document_failures_are_safe(mode):
    payload = _payload()
    table, document, definition = payload["assets"]
    if mode == "columns":
        table["schema"][1]["name"] = "sensor_id"
    elif mode == "pointers":
        document["pointer"] = table["pointer"]
    elif mode == "field-id":
        table["schema"][1]["field_id"] = True
    elif mode == "nullable":
        table["schema"][0]["nullable"] = "unknown"
    elif mode == "native-type":
        table["schema"][0]["native_type"] = PRIVATE_TEXT + "\n"
    elif mode == "foreign-key":
        table["observations"]["foreign_keys"] = [{"columns": ["sensor_id"],
            "target_pointer": "tables/readings", "target_columns": [PRIVATE_TEXT]}]
    elif mode == "count":
        table["observations"].update(row_count=None, row_count_basis="exact")
    elif mode == "clock":
        table["observations"]["source_observed_at"] = "2026-10-03T12:00:00"
    elif mode == "definition-identity":
        duplicate = deepcopy(definition)
        duplicate["pointer"] = "definitions/another"
        payload["assets"].append(duplicate)
    elif mode == "definition-kind":
        definition["upstream_definition"]["kind"] = PRIVATE_TEXT
    elif mode == "definition-reference":
        definition["upstream_definition"]["reference"] = "https://catalog.example/item?token=" + PRIVATE_TEXT
    elif mode == "definition-schema":
        definition["schema"] = deepcopy(table["schema"])
    elif mode == "document-coverage":
        document["document_coverage"]["extraction_truncated"] = True
    elif mode == "missing-coverage":
        document.pop("document_coverage")
    elif mode == "document-locator":
        document["document_terms"][0]["source_locator"].update(line_start=5, line_end=3)
    else:
        document["document_terms"][0]["excerpt"] = PRIVATE_TEXT * 20
    report = _check(payload, retain_semantic_samples=True)
    assert report == failure_receipt("adapter_response_invalid")
    assert PRIVATE_TEXT not in json.dumps(report)


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_legacy_versions_do_not_import_semantic_definition_authority(version):
    payload = _payload() | {"protocol_version": version}
    assert _check(payload, protocol_version=version) == failure_receipt("adapter_response_invalid")


def test_safe_cli_reads_without_writes_credentials_or_network(cli, tmp_path, monkeypatch, capsys):
    path = tmp_path / (PRIVATE_TEXT + ".json")
    raw = _raw(_payload())
    path.write_bytes(raw)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Offline CLI must not read credentials or contact a source")

    monkeypatch.setattr(external_adapter, "read_source_secret", unexpected)
    monkeypatch.setattr(external_adapter.httpx, "Client", unexpected)
    code = cli.main(["--input", str(path), "--provider", "enterprise", "--scope", "catalog",
                     "--protocol-version", "1.2", "--retain-semantic-samples"])
    output = capsys.readouterr()
    assert code == 0 and json.loads(output.out)["result"] == "passed" and output.err == ""
    assert PRIVATE_TEXT not in output.out
    assert path.read_bytes() == raw and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("mode,code", [("missing", "input_unavailable"), ("symlink", "input_unavailable"),
    ("directory", "input_not_regular"), ("fifo", "input_not_regular"), ("large", "input_too_large")])
def test_cli_file_boundary_never_blocks_or_discloses_paths(cli, tmp_path, capsys, mode, code):
    path = tmp_path / PRIVATE_TEXT
    if mode == "symlink":
        path.symlink_to(tmp_path / "not-present")
    elif mode == "directory":
        path.mkdir()
    elif mode == "fifo":
        os.mkfifo(path)
    elif mode == "large":
        path.write_bytes(b" " * (external_adapter.MAX_RESPONSE_BYTES + 1))
    result = cli.main(["--input", str(path), "--provider", "enterprise", "--scope", "catalog"])
    captured = capsys.readouterr()
    assert result == 1 and json.loads(captured.out) == failure_receipt(code)
    assert PRIVATE_TEXT not in captured.out + captured.err


def test_cli_argument_errors_and_internal_exceptions_are_redacted(cli, tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main(["--input", PRIVATE_TEXT, "--provider", "enterprise", "--scope", "catalog",
                  "--protocol-version", PRIVATE_TEXT])
    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out) == failure_receipt("invalid_arguments")
    path = tmp_path / "response.json"
    path.write_bytes(_raw(_payload()))

    def unexpected(*_args, **_kwargs):
        raise RuntimeError(PRIVATE_TEXT)

    monkeypatch.setattr(cli, "check_response", unexpected)
    assert cli.main(["--input", str(path), "--provider", "enterprise", "--scope", "catalog"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == failure_receipt("conformance_internal_error")
    assert PRIVATE_TEXT not in output.out + output.err


def test_failure_receipt_never_echoes_unknown_codes():
    assert failure_receipt(PRIVATE_TEXT)["error_code"] == "conformance_internal_error"


def test_invalid_configuration_still_refuses_before_profile_io(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid request must refuse before profile IO")

    monkeypatch.setattr(external_adapter, "_profile", unexpected)
    with pytest.raises(ConnectorError) as failure:
        external_adapter.discover({"secret_ref": PROFILE_REFERENCE, "scope": "catalog", "asset_names": None})
    assert failure.value.code == "invalid_asset_selection"
