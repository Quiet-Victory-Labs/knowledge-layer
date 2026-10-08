# Read exact staged discovery from a customer harness

A harness can discover current inventory scopes, page their records and retrieve
a small exact selection with all its referenced evidence. It need not know scope
hashes in advance or load an entire company catalog. These operations read the
Knowledge Layer registry; they perform no upstream source queries or writes.

This path preserves discovery material, including native definitions. It does
not turn those definitions into reviewed semantic contracts or company
instructions. Inventory payloads are opaque metadata objects; their vendor
meaning and provenance need separate validation. Large-estate semantic
curation/publication still uses separately bounded, approved scopes.

An agent can now [cite exact inventory evidence](inventory-semantic-citations.md)
in a proposal for an existing snapshot-backed dataset or metric. Local review
governs that interpretation; native-only concept publication remains separate.

## Consumer workflow

1. Resolve a source connection visible to the current principal.
2. Call MCP `get_inventory` with `operation: "scopes"` and `payload: {"limit": 10}`.
   Follow `next_cursor` with `payload.after` to discover further current scopes.
3. Use a returned manifest's `id` with `operation: "page"` to inspect bounded
   entity pages. Choose exact entity IDs from those records.
4. Call `operation: "select"` with `inventory_id`, the returned
   `expected_manifest_digest` and 1–20 distinct `entity_ids` in `payload`.
5. Inspect exact selected entities and same-manifest evidence. Reload discovery
   after a manifest conflict; do not silently substitute another revision.

The compact and advanced MCP surfaces expose the same reader tool even when
source management is disabled. HTTP routes are relative to the authenticated
agent API mount; local browser routes use `/api/knowledge`:

| Operation | Agent HTTP route |
| --- | --- |
| Discover current scopes | `GET /sources/{connection}/inventory/scopes?limit=10` |
| Inspect entities | `GET /sources/{connection}/inventory/{inventory}/page?collection=entities&limit=100` |
| Exact current selection | `POST /sources/{connection}/inventory/{inventory}/select` |

For HTTP selection, put the manifest ID in the route and use this body, replacing
the digest and IDs with values from discovery:

```json
{
  "payload": {
    "expected_manifest_digest": "EXACT_RETURNED_MANIFEST_SHA256",
    "entity_ids": ["EXACT_RETURNED_ENTITY_ID"]
  }
}
```

The example values are placeholders, not accepted handles. Callers cannot
supply a tenant, principal, worker claim, approval or execution authority.

## Completeness and currentness

`scopes` returns at most 20 current scope manifests per response, with a source-
and tenant-bound cursor. An empty `items` list establishes no current manifest
for that source; it is not proof of a complete empty source inventory. A completed
empty scope has its own committed manifest and exact zero counts. Scope paging
is a current listing across calls, not a frozen estate snapshot.

Selection requires a committed current manifest and its exact digest. It
returns complete records without clipping: 1–20 entities, up to 200 unique
referenced evidence records, at most 64 KiB per record and 256 KiB for the full
response. Evidence can have arrived on another ingestion page, but must belong
to the same manifest. Duplicate/missing references, altered stored digests,
staging/aborted/historical manifests and byte-budget excess refuse the whole
selection. Request a smaller selection when the full packet exceeds its budget.
Entity/evidence reads use one-record streaming batches; PostgreSQL qualification
observes actual server cursors. The 256 KiB limit is the serialized response
budget, not a promise about total process RSS or Python object overhead.

The response identifies `knowledge-inventory-selection-v1`, complete text and
the manifest/generation pins. Unknown native versions and observation tokens
stay unknown; zero, empty string and null stay distinct. Exact retrieval does
not certify source authenticity or a frozen upstream observation window.

Current source approval, identity, metadata grants, source failure state and
manifest generation are checked before retrieval and again after response
assembly. PostgreSQL current-selection reads explicitly use READ COMMITTED so
the final check observes changes committed during assembly even when an
operator configures a repeatable-read default. The configured engine/pool and
mutation isolation are preserved. This uses PostgreSQL's statement-level
visibility and SQLAlchemy's connection-scoped isolation reset.
[PostgreSQL isolation](https://www.postgresql.org/docs/current/transaction-iso.html),
[SQLAlchemy isolation](https://docs.sqlalchemy.org/en/21/core/connections.html#setting-transaction-isolation-levels-including-dbapi-autocommit).

These checks do not recall already delivered content or promise atomic
authorization at a later source execution. Revalidate current approved meaning
and permissions before using the harness's separately authorized source tools.
Native document/field ACLs are not supplied by this registry path: split
connections at permission boundaries and authorize the whole retained source.

## Authority and qualification

Every selected packet reports `source_text_authority: "untrusted_evidence"`,
`meaning_certified: false`, `agent_ready: false` and
`source_reads_executed: false`. Source text and an upstream approval label
cannot grant instructions or publication rights. Selection never updates a
legacy snapshot, publication or source-refresh clock. Use the existing
[semantic proposal/review workflow](semantic-contract-agent-loop.md) for
approved meaning and [company guidance](company-guidance.md) for instructions.

Focused tests cover full Unicode/opaque payloads, exact cross-page proof
resolution, source/scope isolation, budget refusal and changes during response
assembly over HTTP and both MCP registrations. The registered
`scripts/drill_inventory.py` also verifies actual PostgreSQL persistence,
revocation and successor commits under a configured repeatable-read reader,
restored isolation, unchanged read-only source fixtures and owned-row cleanup.
Those are local fixture checks; customer ACLs, provider completeness and
estate-scale semantic projection remain separate qualifications.

See [bounded inventory ingestion](bounded-agent-consumption.md#paged-discovery-ingestion)
for staging and complete-scope commit, and [offline adapter conformance](adapter-conformance.md)
for the separate complete-response connector protocol.
