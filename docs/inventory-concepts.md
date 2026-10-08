# Primary concepts from catalog inventory

An enterprise may connect an existing glossary or catalog before it connects a
warehouse. A customer harness can now propose a local business term, entity,
metric definition or data product directly from that inventory. Two small
registry tables retain immutable proposals and separate steward publications.
They reuse source grants, owner decisions, proposal quotas, archive and audit.
No model dependency, vendor SDK or source data write is added.

## Customer harness workflow

1. Use `get_inventory` to discover current scopes and select an exact entity
   with its complete linked evidence. Treat native text, approvals, policies and
   formulas as untrusted evidence.
2. Call `curate_inventory` with `operation: "propose_concept"` and a payload
   containing `name`, `definition`, `concept_kind`, `rationale` and `evidence_ref`.
   The reference has exactly `connection_id`, `inventory_id`, `manifest_digest`,
   `entity_id`, `evidence_id`, taken from that selection. It must cite this source.
   Kinds are `business_term`, `entity`, `metric_definition`, `data_product`.
   Names allow 240 characters; definitions 4,000; rationales 8–1,000.
3. An authorized owner discovers drafts through the exact inventory concepts
   page with `include_drafts: true` and records an approve/reject decision.
   A steward separately publishes the exact reviewed `proposal_digest` with
   a rationale. Agent MCP/HTTP exposes proposals, without review/publication
   routes. Browser review/publication uses its organization role and CSRF gates.
4. Read published meaning with `get_inventory`, `operation: "concepts"`, and
   `inventory_id`, `expected_manifest_digest`, optional `query`, `after`, `limit`.
   Pages return at most 20 entries and 256 KiB. Query is a literal case-insensitive
   name/definition substring. Cursors bind tenant, source, manifest, query and
   draft mode. Follow `next_cursor`; an empty page is distinct from an unknown
   or inaccessible inventory.
5. Retrieve one exact handle using `get_inventory`, `operation: "concept"`,
   `payload: {"concept_id": "RETURNED_LOCAL_CONCEPT_ID"}`. Advanced MCP
   `get_concept_context` and HTTP `/context?concept_id=...` also preserve the
   complete definition. The packet has `basis: "inventory"`, exact native proof
   pins and local review/publication provenance, without a snapshot ID.
6. Use global `search_knowledge` or scoped term `resolve_context` to discover
   published meanings. Results keep distinct handles for conflicting definitions.
   Search considers at most 256 authenticated metadata candidates and resolves
   at most 20 exact inventory packets (10 direct term matches and 10 suggestions;
   global search returns at most 10). Flags describe an incomplete candidate
   budget; they do not claim full recall. Suggestions require scope confirmation.
7. Call `get_task_context` with a query and 1–8 selected `connection_ids`.
   Optionally freeze exact meanings with `inventory_selection`, a list of
   `{"connection_id": "RETURNED_SOURCE_ID", "concept_ids": ["RETURNED_LOCAL_CONCEPT_ID"]}`.
   Every selection must belong to a selected source; select 1–20 distinct concepts
   in total. An empty provided selection refuses. Without handles, bounded term
   lookup may find current meaning or return `no_direct_match`. Broad requests
   that exhaust the operation's exact-citation budget must be narrowed.
8. Revalidate the signed receipt immediately before relying on its context.
   Source execution remains a separate authorization of the customer's own tools.
   The platform has executed no warehouse SQL by publishing this definition.

HTTP readers live under `/sources/{connection_id}/inventory/{inventory_id}/concepts`
and `/sources/{connection_id}/inventory-concepts/{concept_id}`. The agent endpoint
`POST /sources/{connection_id}/inventory-concepts/propose_concept` accepts the
same payload envelope as inventory staging. Browser API prefixes these paths
with `/api/knowledge` and separately enables `review_concept`, `publish_concept`,
`withdraw_concept` POSTs. Review needs `concept_id`, `decision`, optional rationale;
publication needs `concept_id`, `expected_proposal_digest`, rationale; withdrawal
needs `concept_id`, reason. Withdrawal responses contain only the retirement
handle and time. They remain available to a steward with a manage grant during
a source outage or withdrawn source policy.

## Meaning, freshness and boundaries

Review certifies the owner's local interpretation of the cited observation.
Native approvals and ACLs retain `untrusted_evidence` authority. A metric
definition here is business meaning, not an executable metric contract. Published
packets keep `locally_executable: false`, `native_meaning_certified: false`,
`source_agent_ready: false`, `source_reads_executed: false`. One published term
does not certify the whole catalog. Unknown source clocks remain unknown.

A changed current manifest (including a completed empty successor), source
identity/profile/policy change, error, lost access, active refresh, withdrawn
publication or archived proposal prevents current consumption. Decisions and
publications remain historical. Submit a new proposal and review the fresh
evidence; withdrawn publications cannot be restored by replay. Duplicate names
keep separate handles and definitions. No implicit equality, join or last-wins
merge is introduced.

Final checks re-read permissions, current inventory and local publication after
response assembly. They provide bounded revalidation, not recall of delivered
text or atomic authority across independently changing customer tools.

Inventory-aware task packets use `task-context-v2`. Inventory source pins include
the exact manifest, proposal, entity/evidence digests and current owner decision
and steward publication. Their scope clocks are actual observations; no dataset
snapshot is manufactured. Catalog and dataset pins can coexist on the same
connection. Complete reviewed definitions and authority pins survive HTTP and
MCP serialization. Packets and signed receipts are bounded to 256 KiB and 4 KiB.

`business_meaning_available` means selected local definitions are available. It
does not establish question coverage, authorize joins, or compile a native metric
formula. A mixed packet may report `requires_source_execution` for actual dataset
matches; warehouse execution still belongs to independently authorized customer
tools. A blocked dataset remains blocked even when catalog meaning is available.
Company guidance may be scoped to these connections, but inventory concepts are
not yet supported as exact guidance dependencies. Native source text never
becomes an agent instruction through retrieval.

Receipt signing versions retain their existing meaning: `context-receipt-v1`
is process-local and `context-receipt-v2` uses an operator's shared registry keys.
The old four-field request remains valid for snapshot-only receipts. Explicit
inventory selection adds one signed request field. Final checks re-read all
selected concepts, candidate pools, source bindings, grants and company guidance;
changed or withdrawn context refuses on revalidation. These checks cannot create
an atomic authorization lease across independent customer systems.

Snapshot semantic contracts and RDF snapshots do not project these primary
inventory concepts. Existing dataset assertions may still cite raw
inventory evidence through the [snapshot assertion bridge](inventory-semantic-citations.md).
Native catalog API/ACL qualification, customer deployment and analyst uplift
remain separate from synthetic registry/transport/package checks.

Managed PostgreSQL installs must explicitly upgrade to
`0015_inventory_concepts` before starting the API/worker. Review the
[migration guide](schema-migrations.md) and verify backup restoration first.
Historical snapshot publications retain their original meaning and schema.
