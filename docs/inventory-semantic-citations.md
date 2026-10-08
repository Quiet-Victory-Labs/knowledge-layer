# Use catalog inventory as evidence for local meaning

A customer harness can retrieve a complete native catalog record and cite its
exact observation when proposing a description, metric or other assertion for
an existing snapshot-backed source. This connects catalog import to the existing
review and publication workflow. It adds no model dependency, registry tables,
vendor SDK or source execution permission.

## Harness workflow

1. Discover authorized catalog scopes and select exact records with
   [`get_inventory`](staged-inventory-consumption.md).
2. Inspect the full selected entity and linked evidence. Preserve unknown clocks,
   native policies, definitions and unresolved relationships as evidence.
3. Inspect the target dataset's current snapshot, schema and local evidence.
4. Call existing `propose_contract` with the observed target asset, local
   `evidence_ids`, current `expected_snapshot_id`, typed assertion payload and an
   `evidence_refs` entry with **exactly** these five keys:

   ```json
   {
     "connection_id": "EXACT_CATALOG_CONNECTION_ID",
     "inventory_id": "EXACT_CURRENT_MANIFEST_ID",
     "manifest_digest": "EXACT_RETURNED_MANIFEST_SHA256",
     "entity_id": "EXACT_SELECTED_ENTITY_ID",
     "evidence_id": "EXACT_LINKED_EVIDENCE_ID"
   }
   ```

   These are placeholders. Accepted handles come from discovery. The proof must
   belong to that entity and current manifest. Explain the local interpretation;
   copying a native formula does not establish an executable metric.
5. An authorized reviewer approves the local assertion. The steward separately
   publishes the target after its existing completeness and review gates.
6. Retrieve the current semantic contract, scoped asset contract or federated
   graph. `cited_inventory_evidence` contains compact exact pins and record
   digests attached to the local assertion. Inventory selection retrieves the
   full native payload when needed.
7. Revalidate current meaning and permissions before using the harness's
   separately authorized source tools. Task receipts record registry context
   and grant no execution authority.

Snapshot citations retain their three-key shape. A proposal can combine both
kinds, with at most eight distinct references. Inventory references are distinct
by all five pins: one proof can support several selected entities. Legacy
imported-definition citations retain upstream local review plus target review.

## Meaning and authority

Native text, approvals, policies and formulas remain `untrusted_evidence`.
Review records the target owner's interpretation; it does not certify native
meaning, identity mappings or permissions. Inventory selection still reports
`meaning_certified: false`, `agent_ready: false` and
`source_reads_executed: false`. Citing a catalog creates no catalog snapshot or
publication. RDF and compact graphs preserve typed inventory provenance without
inventing snapshot IDs. Raw citations cannot establish structural joins,
`owl:sameAs`, native ACL equivalence or executable provider formulas.

An existing target snapshot is required for this assertion workflow.
[Primary inventory concepts](inventory-concepts.md) have a separate local
review/publication overlay and do not require a target dataset snapshot. Live
vendor accounts, export completeness, clock semantics and native access
boundaries require separate qualification.

## Currentness and cost

Resolution reuses the exact inventory reader's approval, grant, identity,
profile, current-head, integrity and byte-budget checks. It rechecks current
access and head after selection, including cache hits. A successor, completed
empty successor, source error or withdrawn access suppresses dependent approved
meaning. Target publication becomes ineffective and old receipts fail
revalidation. Submission and review recheck after the target writer lock.
Contracts, plans, guidance and task packets check effective state before return.

An operation allows at most 64 distinct selected packets and 256 currentness
checks. Exact references share a local payload cache; caller mutations cannot
alter it. Each selection retains its 256 KiB response limit. Budget exhaustion
refuses the whole operation and requires a narrower scope. These are serialized
payload/work bounds, not an RSS guarantee.

Final checks provide bounded revalidation across independently changing sources.
They do not promise atomic multi-source delivery, recall delivered text or
authorize later execution. Split connections at permission boundaries until
native field/document ACL qualification exists.

Tests cover real local registry effects, mixed citations, shared proofs, altered
pins, changes during assembly, receipts, budgets and compact/RDF provenance. The
existing native adapter drill adds a disposable PostgreSQL worker-to-local-review
exercise using synthetic exports, distinguishing local target publication from
native catalog certification.

The [dated qualification](inventory-citations-qualification-2026-10-04.md) records
clean regression, actual PostgreSQL worker/review effects and installed-package
evidence separately from customer deployment and vendor qualification.
