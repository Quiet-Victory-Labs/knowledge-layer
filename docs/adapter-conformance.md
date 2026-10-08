# Offline adapter conformance

Contributors can check a captured or synthetic complete adapter response or one
paged inventory response without
starting services, provisioning source credentials, or contacting a vendor. The
check uses the **same response validator and normalizer as online discovery**.
It checks complete-response versions 1.0, 1.1 and 1.2 and the distinct paged
protocol 1.3; the default remains 1.0.

This is wire-contract evidence. It does not establish that the adapter read the
selected source, honored a source ACL, returned an actual complete inventory,
preserved truthful provenance, or used a read-only account. It never approves
meaning, publishes a source, or executes a metric. Those require separate source,
permission, lifecycle and customer qualification.

## Check a response

Supply one explicit local UTF-8 JSON file and the provider/scope/version that the
adapter was asked to serve. Protect captured metadata under the customer's policy.
Do not put credentials, application rows, or customer documents in test fixtures.

```sh
uv run --no-sync python scripts/check_adapter_contract.py \
  --input /path/to/adapter-response.json \
  --provider enterprise_catalog --scope glossary --protocol-version 1.2 \
  --max-assets 100 --max-columns 5000
```

The command reads a regular file through one descriptor. Symlinks, directories,
FIFOs and devices refuse. Input is bounded to 2 MiB before and during the read.
There is no endpoint or credential option, no network requests are made, and no
files or registry/source records are written. The command prints one
bounded JSON receipt. Exit 0 means the supplied response conforms under the chosen
options; exit 1 means input/contract refusal; exit 2 means invalid CLI arguments.

Use `--retain-semantic-samples` only to test the connection's explicit document
excerpt opt-in. The default discards excerpts and reports retained coverage as
unknown/disabled. Terms remain metadata. Typed imported definitions remain observed
upstream meaning even when their upstream review label says approved.

Repeat `--asset-name 'Exact native name'` for an optional local selection. The full
response still validates and must fit the byte/asset/column budgets before selection.
Counts describe the retained projection. Missing or ambiguous selected names refuse;
selection cannot hide invalid unselected assets or reduce upstream discovery work.

## Check one paged response

Protocol 1.3 requires the exact request file sent for that page, including its
request digest, profile/session pins, page index, watermark pin and scan interval.
Supply both local files explicitly:

```sh
uv run --no-sync python scripts/check_adapter_contract.py \
  --input /path/to/inventory-page.json \
  --inventory-request /path/to/inventory-request.json \
  --provider enterprise_catalog --scope glossary --protocol-version 1.3
```

The request file is bounded to 16 KiB and the page to 256 KiB. Both use the same
regular-file boundary described above. The shared strict JSON parser and pure
request/page validators enforce the [paged wire contract](paged-adapter-refresh.md).
The selected provider and scope must match the supplied request. Legacy asset
selection, asset/column budgets and semantic-sample retention flags refuse for
1.3; the request carries its inventory budgets. A request file supplied with a
legacy version also refuses.

A valid continuing page reports `listing.state: continuing_page`; a valid terminal
page reports `declared_terminal_page`. Counts describe only `supplied_page`.
Both report `full_page_validated: true` and `inventory_complete_verified: false`.
The checker has no preceding pages or registry head to validate an entire scan.
An empty terminal page is valid wire data, without proving a source was queried.

The page receipt reports an unknown or declared watermark state and whether a
continuation watermark pin was checked. It includes neither cursor nor watermark
value, source/profile identity, entity payload or evidence text. The canonical
scan interval is checked, while `clock_live_validated` remains false: a historical
captured page can pass offline validation. Provider cursor ownership/expiry, live
source permissions, source completeness and semantic publication remain unverified.

```python
from knowledge_layer.adapters.conformance import check_inventory_page

receipt = check_inventory_page(response_bytes, exact_request)
assert receipt["result"] == "passed"
assert receipt["qualification"]["inventory_completion"] is False
assert receipt["scan"]["clock_live_validated"] is False
```

For exact payload comparisons, use
`knowledge_layer.connectors.external_inventory.validate_inventory_page` with the
same request. That pure helper returns the complete untrusted page and raises
`ConnectorError` on refusal. Protect its return value under the customer's policy.

## Receipt and limits

For versions 1.0–1.2, the `adapter-conformance-v1` receipt includes aggregate asset/column/document/import
counts, SHA-256 digests of the exact supplied bytes and canonical request selectors,
byte/count limits, declared completeness scope, and explicit document extraction and
retention complete/incomplete/truncated/unknown counts. A response can be valid wire
data while declaring partial document extraction; the receipt preserves that state
and cannot establish publication readiness. A complete empty listing reports zero
assets. An accepted 1.0 response that omits the completeness flag reports
`legacy_implicit`; `declared_complete` requires an explicit true flag. Both states
validate the full supplied response and leave source completeness unverified.
An unavailable, partial or cursor-bearing listing refuses.

The receipt does not contain source names, paths, pointers, definitions, passages,
row values, or exception messages. Failed receipts contain only a fixed safe code.
Unknown failures are redacted as `conformance_internal_error`. Digests identify
supplied bytes/options, not source authenticity or an upstream freshness guarantee.

The shared parser requires unique JSON keys, strict UTF-8 and finite JSON numbers.
The complete-response versions use the online 2 MiB budget; the paged version uses
its smaller page/request budgets. Rich complete-response versions require explicit `listing_complete: true`.
Parsed strings and keys, including nested extra metadata, must encode as UTF-8;
escaped lone surrogates refuse while valid non-BMP text remains exact.
All existing schemas, field IDs, count bases, same-scope foreign-key target checks,
native provenance, document coverage/locator rules and revision fingerprints apply.
There is no pagination, row profiling, generic SQL or source permission propagation
in this complete-response protocol. See the [protocol](external-connector-protocol.md)
for accepted fields and the separate paged inventory workflow for larger estates.

## Use from an adapter's tests

```python
from knowledge_layer.adapters.conformance import check_response

receipt = check_response(
    response_bytes,
    provider="enterprise_catalog",
    scope="glossary",
    protocol_version="1.2",
    max_assets=100,
    max_columns=5000,
)
assert receipt["result"] == "passed"
```

For tests that must compare exact normalized metadata or revisions, call
`knowledge_layer.connectors.external_adapter.normalize_response` with the same
selectors. It returns the existing normalized discovery dictionary and raises
`ConnectorError` on refusal; it performs no IO. Optional `source_id` and `name`
preserve online registry identity bindings. Do not print this full return value for
customer data: it contains normalized source metadata and opted-in source text.

The bundled [reference adapter](../examples/reference_adapter.py) remains the
backwards compatible synthetic 1.0 example. The optional
[native semantic adapter](../examples/native_semantic_adapter.py) demonstrates 1.2
for bounded dbt artifacts and explicit DataHub glossary URNs. Neither example is a
credentialed vendor qualification. Contributors should cover valid metadata, denied
source access, partial listings, drift, retention off, malformed/sensitive inputs and
their own source-account read-only enforcement before requesting support changes.

```sh
uv run --no-sync pytest -q tests/test_adapter_conformance.py \
  tests/test_enterprise_evidence.py tests/test_upstream_semantic_import.py \
  tests/test_knowledge_connectors_extended.py tests/test_dbt_adapter.py \
  tests/test_datahub_adapter.py
uv run --no-sync ruff check src/knowledge_layer/connectors/external_adapter.py \
  src/knowledge_layer/adapters/conformance.py scripts/check_adapter_contract.py \
  tests/test_adapter_conformance.py
```

The contributor check adds no SDK, broker, model, worker or mandatory service.
