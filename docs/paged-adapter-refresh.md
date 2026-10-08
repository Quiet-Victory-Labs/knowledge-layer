# Paged external adapter refresh

Protocol **1.3** lets an operator-owned adapter supply complete discovery records
over multiple bounded pages. Knowledge Layer stages them in its existing inventory
registry and replaces one scoped head only after the whole scan validates. The
result is untrusted discovery evidence for a harness to interpret. This path creates
no legacy semantic snapshot, approves no business meaning, publishes no source and
executes no SQL, metric or model.

The [complete-response protocols](external-connector-protocol.md) 1.0–1.2 keep their
existing behavior and limits. Version 1.3 requires explicit profile and connection
opt-in; returning a different response version cannot upgrade a legacy connection.

## Configure the boundary

Provision an operator profile through an existing `KNOWLEDGE_SOURCE_*` reference
or its owner-only mounted `_FILE`. Choose one mechanism. The profile file boundary
is a regular, non-followed file of at most 64 KiB.

```json
{
  "endpoint": "https://connector.internal.example/discover",
  "provider": "enterprise_catalog",
  "allowed_scopes": ["glossary"],
  "protocol_version": "1.3",
  "revision": "catalog-adapter-1",
  "source_identity": "catalog-tenant-a-reader"
}
```

An optional `bearer_token` stays in the operator profile. `revision` and
`source_identity` are required nonblank strings of at most 120 characters, without
ASCII controls. They are operator assertions about the adapter and source
principal, not native identity verification. Update them when retargeting the
adapter or changing its effective source principal. A token rotation alone is
excluded from the profile digest; the core cannot infer the permissions behind it.

The endpoint must use `/discover`, with no embedded credentials, query or fragment.
HTTPS is required; the existing explicit insecure-endpoint development opt-in also
applies. Provider/scope identifiers use the existing safe selector syntax, and the
selected scope must appear in the profile's allowlist of 1–100 entries.

Create an `external_adapter` connection with this Python configuration, or send
the equivalent JSON through HTTP/MCP:

```python
{
  "secret_ref": "KNOWLEDGE_SOURCE_ENTERPRISE_CATALOG",  # pragma: allowlist secret - public operator reference, not a credential
  "scope": "glossary",
  "discovery_mode": "inventory",
  "retain_inventory_payloads": True,
  "inventory_limits": {
    "max_pages": 100,
    "max_entities": 10000,
    "max_evidence": 20000,
    "max_total_bytes": 16777216
  }
}
```

The retention flag must be explicitly true. The core preserves the complete
adapter-supplied JSON payload, including any source text it contains. It does not
classify payloads as metadata, sanitize sensitive fields or drop passages. The
operator must provision suitable read-only source access, adapter scope and
audience permissions. Legacy `asset_names`, `max_assets`, `max_columns` and
`retain_semantic_samples` controls refuse in inventory mode.

Omitted inventory limits use the values shown above. Hard ceilings are 10,000
pages, 1,000,000 entities, 2,000,000 evidence records and 256 MiB of total canonical
append packets. Page and total-byte limits must be positive; entity/evidence
limits can be zero for an explicitly empty inventory.

Managed operation additionally requires `external_adapter` to be enabled and an
exact source approval for the tenant/reference/scope/retention identity. The
approval selector includes `discovery_mode: inventory` and
`retain_inventory_payloads: true`. Whole-connection grants govern stored payload
visibility; native row, field and document ACLs are not mirrored. Durable inventory
jobs require a named requester's direct `manage` grant and explicit subject
approval. Group membership alone cannot authorize unattended discovery. Scheduled
jobs also require a current unpaused schedule owned by that subject.

## Request and page fields

Every page uses `POST` to the same operator profile endpoint. Cursors remain JSON
body data: the core never builds a URL from a cursor, follows a response link or
forwards a caller-selected target. Redirects are disabled. A native adapter must
bind its cursor to the approved source, scope, scan and expiration; echo validation
alone cannot prove that a vendor cursor is unstolen or correctly scoped.

The request has exactly these keys:

| Key | Rule |
| --- | --- |
| `protocol_version` | Literal `"1.3"`. |
| `operation` | Literal `"inventory_page"`. |
| `request_id` | Lowercase SHA-256 of the canonical request with this key omitted. |
| `scan_id` | Nonblank session identifier, at most 64 characters. |
| `provider`, `scope` | Operator-selected safe identifiers. |
| `profile_digest` | Lowercase SHA-256 binding endpoint, provider, protocol, sorted scope allowlist, revision and source identity; excludes bearer material. |
| `page_index` | Integer from zero, below `inventory_limits.max_pages` and at most 9999; booleans refuse. |
| `cursor` | `null` only on page zero; otherwise a nonblank opaque string of at most 1024 characters. |
| `scan_started_at`, `scan_expires_at` | Canonical timezone-aware UTC ISO clocks ending in `Z`, with seconds or six-digit microseconds; interval positive and at most 300 seconds. |
| `expected_watermark_digest` | `null` only on page zero; subsequent requests carry the lowercase SHA-256 of the first page's watermark. |
| `inventory_limits` | Exactly `max_pages`, `max_entities`, `max_evidence`, `max_total_bytes`, within the limits above. |
| `max_entities` | Literal integer `100`. |
| `max_evidence` | Literal integer `200`. |
| `max_response_bytes` | Literal integer `262144`. |

Canonical digests use UTF-8 JSON with sorted keys, compact separators, unescaped
Unicode and finite numbers. The request is at most 16 KiB. The generated scan ID,
scan clocks, inventory budgets and target pin remain fixed for one attempt;
page index, cursor, watermark pin and request digest advance per page.

The response has exactly these keys:

```text
protocol_version, operation, request_id, scan_id, provider, scope,
profile_digest, page_index, cursor, scan_started_at, scan_expires_at,
expected_watermark_digest, next_cursor, listing_complete, watermark,
entities, evidence
```

The first twelve fields must echo the request exactly, including JSON types.
`next_cursor` is a bounded opaque string for continuation or `null` for a terminal
page. `listing_complete` is a required boolean and must be true exactly when
`next_cursor` is null. A cursor that fails to advance refuses. Empty continuing
pages still consume page/byte budgets; an empty terminal page is an explicit
empty-scope declaration, not a failed scan represented as zero records.

`watermark` is exactly `{kind, value}`. Kind is bounded nonblank text; a value is
null, a nonnegative integer through `2^63-1`, or bounded nonblank text. An unknown
watermark must be `{ "kind": "unknown", "value": null }`. Zero is preserved as
a known value. The first page establishes the pin and later pages must match it.
A declared watermark is not an independently verified snapshot; unknown watermarks
provide no native snapshot-consistency guarantee.

`entities` has at most 100 records, each exactly `{id, kind, payload, evidence_ids}`.
Kinds are `asset`, `semantic_definition`, `document` and `relationship`.
`evidence` has at most 200 records, each exactly `{id, kind, payload}`. Payloads are
complete JSON objects; records are at most 64 KiB. Entity evidence references are
at most 20 distinct IDs. The registry rejects duplicate IDs within and across
pages, cursor cycles, changed replays, out-of-order pages and unresolved references.
Evidence can arrive on a later page, but every reference must resolve in the same
inventory before commit.

All JSON uses unique keys, strict UTF-8 and finite numbers. Parsed keys and strings
must be valid Unicode, including nested payloads; valid non-BMP text is preserved.
Both the raw response and full canonical append packet, including its three
scope/policy/watermark digests, must fit 256 KiB. Oversized records, pages or total
scans refuse rather than clipping text or silently changing meaning.

## Execution and failure behavior

The orchestrator rechecks source approval, requester grants, schedule/claim and
scan budgets before and after page IO and before commit. Managed writes take a
fresh grant read after obtaining the source lock, using PostgreSQL READ COMMITTED
for those transactions. Operator profile files are reread around source IO and
at registry boundaries; changed target/revision/source-identity pins refuse the
attempt. These checks bound local authority; they do not impersonate an end user
at the adapter or verify its native ACL implementation.

HTTP requests advertise `Accept-Encoding: identity`; compressed responses refuse
before body consumption. The fetcher counts raw streamed bytes and checks the
scan deadline for each chunk and after IO. HTTPX has at most a 15-second operation
timeout and a five-second connection timeout, reduced by the remaining scan
interval at request start. These are transport inactivity limits, not a strict
300-second wall-clock execution guarantee. Registry work, scheduling and an
in-flight read can add time around the checked interval. Adapters must also bound
their own upstream work. HTTP 410 refuses as an expired inventory session.

Retrying a worker attempt starts a new scan ID at page zero. It does not resume a
cursor from a failed or previous attempt. A failed/cancelled/expired scan leaves
the previous complete head in place and cannot stamp a complete-scope clock.
Fenced-out worker staging is retired locally by exact attempt reconciliation;
failed unclaimed staging has a separate owner/digest-bound local retirement path.
Neither cleanup path deletes payloads or changes a committed head. Aborted payload
deletion is a separate [offline operator retention operation](inventory-retention.md).

A successful commit checks counts, complete terminal listing, exact references
and generation CAS, then atomically records the new scoped head, audit and job
result. It stamps only the local `last_complete_scope_at`. Global source success
or observation clocks remain separate, and `source_observed_at` is never invented
from a refresh time. Connection status `inventory_complete` cannot activate a
legacy semantic publication. A harness consumes exact current rows through
[scopes/pages/selections](staged-inventory-consumption.md) and performs cited
interpretation and review before any separate semantic publication.

## Qualification scope

The [offline page checker](adapter-conformance.md) validates one supplied page
and request. Even a terminal page receipt leaves inventory completeness and
live-clock/source/ACL/vendor qualification false. Pure and mocked transport tests,
installed package smoke, disposable native PostgreSQL registry drills and a real
customer adapter are separate evidence layers. Generic synthetic pages or native
registry qualification do not qualify Dataplex, Databricks, Collibra, Alation,
DataHub, Snowflake or another provider account. The
[local worker qualification](paged-adapter-qualification-2026-10-03.md) records
actual PostgreSQL and worker execution separately from the unresolved provider
gate. The capability manifest leaves live paged-adapter qualification false;
these receipts grant no source access or operating SLO.
