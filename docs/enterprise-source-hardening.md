# Source metadata and evidence hardening

This guide describes metadata and evidence boundaries implemented by the alpha.
Private source-specific pilot reports are excluded from the public tree. The
checks below are a validation plan, not a newly executed live qualification.
Discovery, evidence recall, human semantic review, source authorization and
production qualification remain separate claims.

## Implemented changes

| Failure class | Resulting behavior | Other sources affected |
| --- | --- | --- |
| Important policy text disappeared after the first paragraph | Retain bounded exact passages across the document, with source-byte revisions and line/page/paragraph locators; search requires every query term | Markdown, text, HTML, DOCX, text-bearing PDF and adapter documents |
| Partial extraction looked complete | Extraction, passage retention and structured candidate coverage have separate completeness/truncation states; known incomplete evidence blocks publication | Scanned/encrypted PDF, large documents, more than 50 passages or headings, more than 100 threshold candidates |
| Generic type names hid physical schema changes | Retain native types and bind revisions to native schema facts; Iceberg includes nested field IDs, schema ID, snapshot ID and observed table UUID | Precision/scale/width/timezone changes, nested structs, table replacement, schema changes without new data snapshots |
| Missing metadata became a false value | Preserve null/unknown state, count basis and missing/null/empty/zero separately | PostgreSQL unmeasured estimates, absent object sizes, unsupported key reflection, absent nullability, missing SDK-supplied UUID |
| JSON conversion fabricated cardinality or overlaps | Parse decimal numbers exactly; compare numeric equality without rounding, overflow or underflow; retain typed identity for booleans and strings | Large exponents, high-precision IDs, equivalent numeric encodings, heterogeneous identifiers |
| Samples hid later schema changes or malformed records | Discover the field union across every record inside the file byte budget; validate records outside the value-profile sample; refuse duplicate/blank CSV headers, overflow rows, duplicate JSON keys and non-object records | Evolving JSONL event streams, ragged CSV exports, multilingual metadata |
| A measured zero-overlap result disappeared | Persist probe evidence independently of inferred relationships; bind both revisions and retain aggregate counts only | CSV/JSONL key association, sparse or incompatible identifiers |
| Provider-specific assumptions prevented connection | Provision generic Iceberg REST aliases and source profiles; metadata discovery does not require Polaris or AWS credentials | Lakekeeper and other compliant REST catalogs; each auth/deployment still needs qualification |
| A generic SQL driver bypassed access bounds | Verify read-only and timeout policy for PostgreSQL, official ClickHouse and SQLite; refuse other dialects with an adapter path | MySQL, Snowflake, BigQuery, Databricks and other warehouses require a separately qualified policy/adapter |
| Source facts could not reach agents | Optional adapter protocol 1.1 carries field IDs, physical types, schema/snapshot identity, count basis, observation clocks, declared keys and lineage | SaaS APIs, cloud catalogs and mixed source systems without adding every SDK to the core |
| Partial pages or stale receipts looked healthy | Require explicit complete listings in 1.1; refuse continuation cursors and duplicate metadata keys; the replay grades current refresh state and observation timestamps | Paginated APIs, source timeouts, provider deletion inference, cached healthy snapshots |
| Wide schemas silently lost evidence | Keep evidence for all fields within the bounded curation budget; refuse scope overflow explicitly | Enterprise-wide catalogs and large schema/document combinations |

Typed key constraints remain technical observations. A composite primary key does
not prove uniqueness of each component. A numeric name does not supply units,
aggregation or business meaning. Actual/projection point fields now receive
separate metric drafts, with no certified definition or approved join.

## Validate with a synthetic source

Use a disposable registry and the [synthetic reference adapter](../examples/reference_adapter.py).
Select an
explicit bounded namespace and preserve caller-supplied native identity, clocks,
revision and coverage. Unknown values must remain unknown.

Check exact schema/type/field identity, nullable and missing values, duplicate
metadata keys, truncated documents, passage locators and incomplete listings.
Inject a source timeout after a successful refresh; the retained old snapshot
must not become evidence of a healthy current refresh. Changed source revisions
must invalidate affected citations and require review before publication.

The [connector qualification guide](native-connector-qualification.md) describes
native metadata checks. The [local qualification runbook](local-qualification-runbook.md)
describes disposable registry and recovery checks. A synthetic replay cannot
prove customer permissions, account coverage or live-provider reliability.

## Upgrade and qualification boundaries

Adapter protocol 1.0 remains the default. Operators opt into 1.1 in their named
server-side profile. See [the protocol](external-connector-protocol.md) for exact
field limits, observation states and document retention. Local table/document
profile policy version 7 rebuilds old cached profiles. Richer native schema
fingerprints and table identities can invalidate old evidence and require human
re-review; existing approvals are not grandfathered across changed facts.

The SQL provider menu now offers only PostgreSQL, ClickHouse and SQLite. An
installed SQLAlchemy dialect alone does not establish its access policy. Existing
Polaris configuration remains available; generic REST profiles use owner-only
named secrets. See [native qualification](native-connector-qualification.md) for
the installed SDK version, transport bounds and test scope.

Provider network reachability must be qualified in the approved deployment.
GCS data-file reads,
SaaS OAuth delegation, incremental paging, source-native row/document ACL
mirroring, multi-organization isolation, real company IdP deployment, OCR,
customer load/recovery targets and steward-labeled semantic answer quality remain
independent work. This change does not claim those capabilities.
