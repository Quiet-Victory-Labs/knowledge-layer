# External source adapter protocols

Versions **1.0, 1.1 and 1.2** return a bounded complete response as described below.
Version **1.3** uses a distinct [paged discovery refresh contract](paged-adapter-refresh.md)
with explicit complete-payload retention and inventory staging. It does not upgrade
the complete-response normalizer or publish semantic meaning.

Run a source adapter separately from the Knowledge Layer API, using the source's existing read-only access path. In the complete-response protocols, the adapter returns scoped metadata and, after explicit opt-in, short document passages. Source SDKs and credentials stay in the adapter or refresh worker. Application rows, raw join keys, full document bodies, and source query execution are outside these complete-response protocols.

Contributors can use the [offline conformance checker](adapter-conformance.md)
without starting a server or provisioning credentials. It shares the online
validator and reports wire validity separately from source/vendor qualification.

## Operator profile and source approval

Provision this JSON as `KNOWLEDGE_SOURCE_CRM`, or mount an owner-only regular file and set `KNOWLEDGE_SOURCE_CRM_FILE` to its absolute path. Configure one mechanism, not both. The secret resolver limits profiles to 64 KiB.

```json
{
  "endpoint": "https://connector.internal.example/discover",
  "provider": "crm",
  "allowed_scopes": ["accounts"],
  "protocol_version": "1.1",
  "bearer_token": "<operator-provisioned adapter token>"
}
```

The profile owns the outbound URL, provider, scope allowlist, token, and protocol version. A connection stores only its `secret_ref`, `scope`, budgets, optional `asset_names` selection, and `retain_semantic_samples` choice. Endpoint paths must be `/discover`, with no embedded credentials, query, or fragment. HTTPS is required and redirects are disabled. `KNOWLEDGE_ALLOW_INSECURE_ADAPTER_ENDPOINTS=1` permits operator-configured HTTP for controlled development; use a loopback endpoint for the local example.

Omitting `protocol_version` preserves **1.0**. Setting it to **1.1** opts that profile into the richer contract below; **1.2** inherits those metadata and document rules and additionally enables typed upstream semantic definitions. The response must echo the requested version exactly; the adapter cannot upgrade a connection by returning a different version. The bundled [reference adapter](../examples/reference_adapter.py) implements the synthetic 1.0 example and is not a qualified CRM connector.

An explicit **1.3** profile additionally requires operator-managed `revision` and
`source_identity`. It works with an inventory-mode connection and its explicit
`retain_inventory_payloads: true` setting. Legacy snapshot discovery refuses that
profile. See the [paged contract](paged-adapter-refresh.md) for its exact request,
response, authorization, byte and continuation boundaries.

In managed/OIDC operation, enable `external_adapter` and provision an exact [source approval](source-access.md) for the tenant, secret reference, scope, and retention choice. For example, the selector is:

```json
{
  "secret_ref": "KNOWLEDGE_SOURCE_CRM",
  "scope": "accounts",
  "retain_semantic_samples": false
}
```

Operator approval controls which sources may be created and refreshed. Connection metadata grants control who may read the resulting snapshot. The adapter uses its own scoped source principal; the core does not forward an end-user identity or mirror source row, field, or document ACLs. Split source connections at permission boundaries and grant access to the whole stored snapshot only when it is appropriate for that audience.

## Optional exact asset selection

Set `asset_names` in the Knowledge Layer connection configuration to retain a specific inventory within an operator-provisioned scope:

```json
{
  "secret_ref": "KNOWLEDGE_SOURCE_CRM",
  "scope": "accounts",
  "asset_names": ["Accounts"],
  "max_assets": 100,
  "max_columns": 5000,
  "retain_semantic_samples": false
}
```

The optional list contains 1–100 unique exact asset names, each 1–120 characters without ASCII control characters and containing non-whitespace text. Matching preserves case, Unicode, and surrounding whitespace; the core does not trim or normalize source names. List order does not change the selection. Omit the key to retain the existing full-scope behavior; `null` and an empty list are invalid selections.

Selection only reduces the inventory returned by the approved adapter scope. The core does **not** forward `asset_names` upstream or change the adapter's source principal. It retrieves and validates the full response before filtering, including complete-listing rules, schemas, observations, document metadata, foreign-key target fields, and the response-byte, asset, and column budgets. Invalid unselected metadata can therefore fail the refresh. Selecting one asset does not reduce upstream discovery work or allow a larger provisioned inventory to bypass these limits.

Every requested name must resolve to exactly one validated asset. A missing name refuses the whole selection with `adapter_asset_not_found`; a requested name shared by multiple assets refuses with `adapter_asset_name_ambiguous`. Invalid list configuration refuses with `invalid_asset_selection`. The core never returns only the requested names it happened to find. With no selection, otherwise valid assets sharing a name retain the existing behavior; their pointers must still be distinct.

Selected assets retain their complete fields, observations, source pointers, and individual revisions. The curated snapshot records the selection boundary in `source.asset_selection`:

```json
{
  "asset_names": ["Accounts"],
  "complete": true,
  "completeness_scope": "requested_asset_names"
}
```

Here `complete` means every requested name resolved uniquely. Discovery completeness describes that requested inventory; it does not declare an enterprise-wide inventory complete, approve business meaning, or publish the source. Foreign-key declarations remain on selected assets, but a target excluded from the selection is outside the retained model and remains unresolved there.

The source revision binds the canonical name selection and the selected asset revisions. Adding or removing names changes that identity, including choosing an explicit selection containing every currently returned asset. Reordering the same list does not. Changes to unselected assets alone leave selected content identity stable when the full response still validates and the selected facts remain unchanged. An explicit registry refresh still records a new snapshot version under the existing lifecycle; stable content identity is separate from that version counter.

## Request and complete listing

The core sends `POST /discover` as JSON, with the profile's bearer token in `Authorization` when configured:

```json
{
  "protocol_version": "1.1",
  "scope": "accounts",
  "max_assets": 100,
  "max_columns": 5000,
  "include_document_excerpts": false
}
```

`max_assets` is 1–100. `max_columns` is 1–5000 **across all assets**, not per table. The core uses a 15-second HTTP operation timeout with a five-second connect timeout and caps the decoded response at 2 MiB. The adapter must also bound its source work and enforce the requested scope. Return HTTP 200 only for a completed metadata listing.

Versions 1.1 and 1.2 require `listing_complete: true`. Versions 1.0–1.2 have no pagination: omit `next_cursor` or set it to `null`. A partial listing, non-null cursor, excess assets, or excess columns fails the refresh; narrow the scope instead of returning a clipped inventory. Version 1.0 accepts an omitted `listing_complete` as its legacy complete-response assertion, but still rejects an explicit incomplete flag or non-null cursor. An empty complete listing means the scoped scan completed and found no assets; an unavailable scan must fail. Protocol 1.3 is a separate explicit paged contract; these legacy rules remain unchanged.

JSON object member names must be unique. The core rejects duplicate keys in the response rather than allowing a later value to replace an earlier one. Valid protocol metadata can still exceed a separate curation budget; the core refuses that scope rather than silently publishing a partial candidate set.

## Upstream semantic definitions in 1.2

An operator profile explicitly negotiated at **1.2** can return `semantic_definition` assets alongside its tables and documents. This generic protocol imports existing catalog, glossary, ontology, metric, semantic-model, and data-product definitions without requiring a vendor SDK in the core. A concrete adapter still needs independent qualification against its upstream system.

```json
{
  "name": "Revenue",
  "pointer": "definitions/retail/revenue-17",
  "asset_type": "semantic_definition",
  "schema": [],
  "upstream_definition": {
    "id": "native-revenue-17",
    "kind": "metric",
    "namespace": "retail",
    "definition": "Booked sales excluding tax and returned orders.",
    "version": null,
    "source_observed_at": null,
    "owner": "Retail finance",
    "authority": "Retail business glossary",
    "reference": "https://catalog.example/terms/revenue-17",
    "review_status": "published",
    "unit": "USD",
    "formula": "booked_sales - tax - returned_orders"
  }
}
```

`upstream_definition` has exactly the required keys `id`, `kind`, `namespace`, `definition`, and `version`, plus any of the optional keys shown. Its native name comes from the asset's `name`. Kinds are `catalog_asset`, `glossary_term`, `ontology_term`, `metric`, `semantic_model`, and `data_product`. `id` is bounded to 256 characters; `namespace` to 120; definition and formula to 4000; native version to 128; owner, authority, review status, and unit to 120. Non-null text must contain non-whitespace content and no ASCII controls; definition/formula may contain tabs and line breaks. Text is preserved exactly.

The version key is required, but `null` represents an unavailable native version. Optional missing/null provenance stays `null`. A provided observation clock must be timezone-aware ISO 8601 with at most 40 characters; refresh completion is never substituted. References may be safe source-relative identifiers or HTTPS permalinks of at most 256 characters, without embedded credentials, query strings, or fragments. These are declared provenance, not independent authority verification.

Semantic assets have empty schemas. Duplicate `(kind, namespace, id)` identities within the approved source scope fail, even if pointers differ. Local identity binds the connection's source, native kind, namespace, and ID; same-named definitions across business units remain distinct. These assets count against the existing complete-listing, asset, response-byte, and curation budgets. Legacy 1.0/1.1 profiles reject semantic definitions instead of silently importing them.

The core binds native definition text, name, formula, unit, version, owner, authority, review status, and reference into the content fingerprint. Changing meaning or provenance changes its evidence even when the vendor reuses a version or has no version. Observation clocks are retained separately and do not fabricate content changes. A source pointer change also invalidates evidence-bound reviews. A successful complete listing that removes an object removes it from the current snapshot; historical snapshots remain available.

Each imported object creates a source-scoped concept with `definition: null`, an `upstream_declared` candidate definition, and revision-bound `upstream_semantic_definition` evidence. An upstream `published`/`approved` review label remains native metadata. It cannot grant Knowledge Layer approval, publication, equivalence, or execution. A steward can use the existing cited semantic-proposal workflow to review a local definition; changed evidence invalidates that decision.

Agent `resolve_context` preserves the exact candidate text and native provenance. Full semantic contracts, and compact `get_contract` for that asset, expose an `upstream_definitions` collection with the native definition, source revision, evidence, observed trust, separate `kl_definition_status`, and `executable: false`. Complete contract transports preserve text or explicitly refuse their response budget. These records do not populate approved executable metrics, compile formulas or graphs, resolve cross-source references, or establish same-name equivalence. Cross-source contract citations accept current document-term evidence or exact typed imported-definition evidence. Imported meaning must have a current local review anchored to that evidence before a dependent source assertion can become effective. Contracts expose these as `cited_upstream_definitions`, preserving native metadata separately from local interpretation. An upstream approval label alone cannot satisfy either local review. Changed imports, source errors, archived reviews and denied grants invalidate the dependent assertion. See [semantic contracts](semantic-contract-agent-loop.md).

## Table metadata and optional observations

This illustrative 1.1 response declares two tables and a technical foreign key. All identifiers and counts are example metadata:

```json
{
  "protocol_version": "1.1",
  "listing_complete": true,
  "assets": [
    {
      "name": "Accounts",
      "pointer": "objects/accounts",
      "asset_type": "table",
      "revision": "schema-2026-10-02",
      "schema": [
        {"name": "account_id", "native_type": "UUID", "nullable": false, "field_id": 1},
        {"name": "annual_revenue", "native_type": "NUMERIC(18,2)", "nullable": null, "field_id": 2}
      ],
      "observations": {
        "schema_id": 3,
        "snapshot_id": 17,
        "source_observed_at": "2026-10-02T12:00:00Z",
        "row_count": 250,
        "row_count_basis": "snapshot_summary",
        "primary_key": ["account_id"],
        "foreign_keys": [],
        "lineage": []
      }
    },
    {
      "name": "Contacts",
      "pointer": "objects/contacts",
      "asset_type": "table",
      "revision": "schema-2026-10-02",
      "schema": [
        {"name": "contact_id", "data_type": "text", "nullable": false},
        {"name": "account_id", "data_type": "UUID", "nullable": false}
      ],
      "observations": {
        "row_count": null,
        "row_count_basis": "unknown",
        "primary_key": ["contact_id"],
        "foreign_keys": [
          {"columns": ["account_id"], "target_pointer": "objects/accounts", "target_columns": ["account_id"]}
        ]
      }
    }
  ]
}
```

Asset names and column names contain 1–120 characters with no ASCII control characters. `asset_type` defaults to `table` and may be `table`, `document`, or `file`. Each asset pointer must be unique, 1–256 characters from `A–Z a–z 0–9 _ . : / @ # -`, with no `://` or `..` path segment. Use a stable source-relative identifier, never credentials, signed URLs, row values, or document text. The core projects it under `adapter/{provider}/{scope}/{pointer}`.

Column names must be unique within an asset. `native_type` takes precedence over `data_type`; either supplies the original declared type, bounded to 512 characters without ASCII control characters. The core retains that spelling as `native_type` and derives its generic `data_type` separately. If neither is supplied, the type remains `unknown`. Omitted or null `nullable` remains unknown; it is not converted to `true` or `false`. In 1.1, optional `field_id` is a unique integer within the asset in the range 1–2³¹−1. Version 1.0 retains native type and unknown nullability but does not project field IDs or the 1.1 observations.

`observations` is optional in 1.1. Only the following keys are accepted:

| Key | Accepted value and interpretation |
| --- | --- |
| `schema_id`, `snapshot_id` | Null or integer 0–2⁶³−1. Missing/null is not replaced with a synthetic ID. |
| `schema_fingerprint` | Exactly 64 lowercase hexadecimal characters, representing the adapter's observed schema fingerprint. |
| `source_observed_at` | Timezone-aware ISO 8601 timestamp of at most 40 characters. Supply the source observation time; do not substitute refresh completion time. |
| `row_count`, `row_count_basis` | Null with `unknown`, or a nonnegative integer through 2⁶³−1 with `exact`, `estimate`, or `snapshot_summary`. Omitted count/basis becomes null/`unknown`. Zero is a measured count, not a missing value. Contradictory pairs fail. Only `exact` sets the core's `row_count_is_complete` flag. |
| `primary_key` | Null or 1–32 distinct column names from this asset. A declaration is technical metadata, not a sampled uniqueness proof or approved business grain. |
| `foreign_keys` | At most 50 objects, each with exactly `columns`, `target_pointer`, and `target_columns`. Both column lists have 1–32 distinct names and equal lengths; source columns must exist. |
| `lineage` | At most 50 valid source-relative pointers. Duplicate pointers are deduplicated. These are adapter declarations, not verified transformation results or business equivalence. |

The core validates foreign-key target columns against every target present in the full adapter response before asset selection. Technical relationships then resolve only within the retained inventory of the same adapter source and scope. A target outside that inventory remains unresolved. A resolved declaration may produce an `observed_metadata` technical relationship; it does not approve a business join, identity crosswalk, metric grain, or cross-source equivalence. Lineage pointers also do not grant access to another connection.

The bridge remains metadata-only: it does not fetch rows or produce null/distinct counts, sampled join overlap, or source-native ACL proofs. An adapter may report a bounded aggregate count with its basis without transferring the rows behind it.

## Document passages and coverage

For a `document` asset, return at most 50 `document_terms` entries. A term is 1–96 characters without ASCII control characters. It may represent a heading or other explicitly identified source term. Send an excerpt only when `include_document_excerpts` is true; the connection's `retain_semantic_samples` choice defaults to false. The core retains excerpts only with that opt-in.

```json
{
  "name": "Billing policy",
  "pointer": "documents/billing-policy.md",
  "asset_type": "document",
  "revision": "content-sha256-or-source-version",
  "schema": [],
  "document_coverage": {
    "extraction_complete": true,
    "extraction_truncated": false,
    "retention_complete": true,
    "retention_truncated": false
  },
  "document_terms": [
    {
      "term": "Billable hours",
      "excerpt": "Technician labor hours belong to one work order.",
      "line": 8,
      "source_locator": {
        "format": "md",
        "coordinate_basis": "source_lines",
        "line_start": 8,
        "line_end": 8,
        "extracted_line_start": 8,
        "extracted_line_end": 8,
        "column_start": 1,
        "column_end": 48
      }
    }
  ]
}
```

The excerpt must be actual source text, 4–280 characters in its original form, with at least four characters remaining after stripping outer whitespace. The core preserves the exact original excerpt, including outer whitespace. Its locator must identify that preserved passage, not an adapter-generated explanation. `line` is a legacy optional positive integer through 100000. Omitted or invalid legacy line values become unknown.

In 1.1, retaining passages requires `document_coverage` with **exactly** the four boolean flags shown above. Complete extraction cannot also be truncated. Complete retention requires complete extraction and cannot be truncated. Unavailable text can be `extraction_complete: false, extraction_truncated: false`; do not label it an empty or complete document. With retention disabled, the core stores retention completeness as unknown and does not retain the excerpts. Known incomplete extraction, or incomplete retention when enabled, makes curation partial and blocks source publication. A 1.0 document remains usable, but missing coverage and precise locators remain unknown.

`source_locator` is optional and interpreted only for a retained 1.1 excerpt. Supported formats are `md`, `markdown`, `txt`, `text`, `html`, `htm`, `docx`, and `pdf`. Supported coordinates are `line_start/end`, `extracted_line_start/end`, `source_line_start/end`, `source_node_line_start/end`, `column_start/end`, `page` or `page_start/end`, and `paragraph` or `paragraph_start/end`, plus a short `coordinate_basis` string. Unsupported locator keys fail validation. Coordinates are integers through 1000000; non-column coordinates start at 1 and columns may start at 0 for producer compatibility. Start/end ranges must be ordered. Multi-line passages may end at a lower column than their first-line start; single-line column ranges must be ordered.

For new adapters, supply complete one-based, inclusive line/column coordinates and a declared basis. The local extractor's `source_lines` locators identify Markdown/text characters directly. HTML uses `html_source_node_start_line_and_rendered_text_line`: source-node lines are coarse parser anchors, while columns refer to rendered extracted text. PDF uses page plus extracted-text lines, and DOCX uses paragraph plus extracted-text lines. These are text-extraction coordinates, not OCR, image geometry, or a guarantee that rendered lines equal raw source lines. A missing or partial locator cannot establish an exact source span. The core validates the locator structure; the adapter remains responsible for its accuracy against the source revision.

## Revisions, evidence, and limits of approval

An optional adapter `revision` is a 1–128 character token using letters, digits, `_ . : -`. Change it when the source metadata or document changes. The core also binds the asset revision to the observed schema, native types, field IDs, 1.1 observation facts, and retained document terms, passages, locators, and coverage. A reused adapter revision therefore cannot conceal a changed schema or changed excerpt. `source_observed_at` is excluded from this content fingerprint so a new observation clock alone does not fabricate a content change.

Document passages receive evidence handles tied to the source asset revision. Search returns current authorized headings/passages, source pointers, revisions, and portable `{connection_id, snapshot_id, evidence_id}` references. A missing match describes the retained search corpus, not proof that the full source lacks the term. Inferred concepts, synonyms, metric definitions, and joins still require source-grounded review; successful discovery or an adapter declaration does not certify business meaning or publish a source.

Provider failures are exposed as bounded public error codes, without forwarding provider exception text. The adapter must apply source authorization and remove sensitive content before sending permitted excerpts. The protocol provides no OAuth delegation, incremental cursor, source query execution, source ACL mirroring, or OCR claim. Deploy an adapter only for the sources an enterprise uses and qualify its real account, grants, denied scopes, drift, and failures independently. Verify the restricted source account independently of any driver read-only setting; that setting is not a general guarantee about every SQL or administrative operation.

## Local document extraction uses the same evidence boundary

The local-file connector accepts Markdown, UTF-8 text, HTML/HTM, DOCX, and text-bearing PDF. Invalid UTF-8 is refused rather than stored as replacement text. Text extraction is bounded to 50000 characters and 4000 lines; PDF extraction additionally examines at most eight pages and applies parser resource guards. An image-only/scanned PDF reports `no_text_or_ocr_required` with incomplete text coverage.

With retention enabled, meaningful paragraphs become at most 50 passages of 280 characters per document, with source locations and small boundary overlaps counted inside that same limit. Heading/filename candidates are limited to 50; recognized numeric threshold candidates are limited to 100. `document_extraction`, `document_retention`, and `document_candidates` report their separate coverage and truncation states. A text scan can be complete while candidate or retention coverage is partial. Known incompleteness blocks publication; disabling retention intentionally keeps heading/filename metadata without body excerpts or numeric threshold values.

Document/PDF profile policy version 7 rebuilds older cached profiles even when file bytes are unchanged. File SHA-256 revisions, locator-bearing passage evidence, and snapshot references preserve provenance across refreshes. These local extraction limits are not a promise that an external adapter scanned its entire upstream corpus: the adapter must declare its own truthful coverage within its approved scope.
