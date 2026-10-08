# Use reviewed company meaning from a customer harness

Knowledge Layer supplies context and parameterized plans. The customer's harness
executes its own authorized database reads and checks the results. No model API
key, query service, vector database or mandatory agent runtime is required.

## Run the synthetic enterprise journey

From a source checkout:

```sh
uv sync --frozen
uv run --no-sync python examples/enterprise_consumer.py
```

The example creates two SQLite business units and a policy document in an
isolated temporary directory. Fixture setup performs explicit steward review and
publication. Its consumer then uses the compact MCP tools to load company
instructions, retrieve both units' `Revenue` meanings, revalidate the context,
plan each metric and execute independently authorized read-only SQL.

For actual CLI consumption, use the separate opt-in
[native harness drill](native-harness-qualification.md). Its reference Codex
run additionally verifies HTTP MCP calls and withdrawal; it does not qualify
customer OAuth, native skills or customer source permissions.

Finance excludes canceled orders and reports **20 USD** of known booked revenue,
one observed zero and one missing amount. Sales includes open opportunities and
reports **1,040 USD**. Their populations overlap, so the example leaves the combined
company total unknown. Withdrawing finance publication invalidates the held
receipt. Fixtures and registry are removed on exit. This verifies the local tool
journey; it does not measure model uplift or qualify a named commercial harness.

## Consumer operations

| Compact and advanced MCP tool | Agent HTTP route | Purpose |
| --- | --- | --- |
| `get_capabilities` | `GET /capabilities` | Dated supported scope and evidence limits |
| `get_task_context` | `POST /task-context` | Select 1–8 authorized sources and one bounded term/task |
| `revalidate_task_context` | `POST /task-context/revalidate` | Recheck principal, policy, meaning, guidance and source pins |
| `plan_metric_query` | `POST /sources/{id}/metric-plan` | Current approved assertion → parameterized SQL and required checks |
| `plan_relationship` | `GET /sources/{id}/relationships/{assertion}/plan` | Reviewed exact bridge → identity/cardinality/coverage obligations |
| `get_maintenance_work` | `GET /maintenance-work` | Version-bound interpretation work and separate freshness stages |

HTTP routes are relative to the authenticated agent API mount. Browser routes
for these operations live under `/api/knowledge/consumer`. Authentication and
source grants remain required in OIDC mode; the local demo profile is scoped to
its operator-approved installation.

A task request uses explicit source IDs:

```json
{"query":"Revenue","connection_ids":["finance-source","sales-source"]}
```

Resolve context, inspect its blockers and selected cohorts, choose an approved
assertion, and revalidate the receipt immediately before planning/execution. The
planner derives table, dialect and approval from the current registry. A caller
cannot supply a replacement SQL table or approval flag. Example metric request:

```json
{"assertion_id":"reviewed-revenue-assertion","request":{"group_by":["region"],"limit":100}}
```

The current finite metric language supports explicit units, dimensions, typed
filters, cohort keys, time column/grain/timezone, additivity, numeric expressions
and aligned aggregate ratios. PostgreSQL, ClickHouse and SQLite have scoped SQL
renderers. Other dialects and unsupported native formulas refuse. A row limit
bounds returned rows; it does not bound source scans. Inspect source-native
EXPLAIN/permissions, current schema, grain, precision, truncation, missingness and
zero-denominator checks before presenting a computed answer. All-null results
remain unknown; empty cohorts and observed zero remain distinct.

Cross-source joins require independent evidence, namespaces, exact native fields
and current effective publication on both sides. Reviewed cardinality is a
claim to check, not measured coverage. Fanout blocks until an explicit result
grain is established. Temporal/composite cross-engine execution and automatic
currency conversion are outside this release's supported compiler scope.

Receipts expire after five minutes. The default process-local key also refuses
after restart or on another process. Operators can enable a
[shared registry key ring](context-receipt-keys.md) for restart/replica continuity,
with explicit key rotation and revocation. Every accepted receipt still checks
current permissions and meaning. A valid receipt grants no source permissions
and cannot prove another harness followed a rule. Dependency reads refuse beyond
64 snapshot projections per outer read or a cycle/depth violation, rather than
returning a partial approved graph.

Revalidation refuses blocked context even when its signed registry pin is
unchanged. A successful check reports `context_status` and `answerability`;
`no_direct_match` stays `not_established`. Failed or incomplete document refreshes
retain the last complete evidence for inspection but suspend dependent reviewed
assertions and publication readiness until their supporting source is current.

## Paged discovery ingestion

A source manager with a direct manage grant can stage inventories through the
operator-enabled agent API or MCP. Mutation tools/routes are absent when source
management is disabled. Discovery never approves meaning or advances the legacy
semantic-snapshot freshness clock.

| MCP tool/operation | Agent HTTP route |
| --- | --- |
| `stage_inventory`: `begin`, `append`, `commit`, `abort` | `POST /sources/{id}/inventory/{operation}` |
| `get_inventory`: `current` | `GET /sources/{id}/inventory/current?scope_digest=...&policy_digest=...` |
| `get_inventory`: `scopes` | `GET /sources/{id}/inventory/scopes?limit=10` |
| `get_inventory`: `select` | `POST /sources/{id}/inventory/{inventory}/select` |
| `get_inventory`: `status` | `GET /sources/{id}/inventory/{inventory}` |
| `get_inventory`: `page` | `GET /sources/{id}/inventory/{inventory}/page?collection=entities` |

Commands use a `payload` object. Begin binds `{scope:{namespace,selectors},
policy:{id,revision},watermark:{kind,value},limits:{max_pages,max_entities,
max_evidence,max_total_bytes}}`. Revisions/watermarks may explicitly be unknown.
Append binds the returned scope/policy/watermark digests, zero-based `page_index`,
`cursor`, `next_cursor`, complete `entities` and `evidence`. Each entity has
`id`, `kind`, full JSON `payload` and exact `evidence_ids`; evidence has `id`,
`kind` and full JSON `payload`. Omit credentials and row samples before ingestion.
Source text remains untrusted evidence.

Commit requires `{inventory_id,completion:{listing_complete:true,pages,entities,
evidence,manifest_digest}}`. The adapter attests its selected scope's completeness;
Knowledge Layer validates the staged manifest, exact counts, references, pins,
owner and worker generation. A source changing while it is observed is not
proven frozen. Native consistency tokens remain provider-specific evidence.

Changed replay, duplicate IDs, cursor cycles, unresolved evidence and stale
commits refuse. Partial pages cannot replace the effective manifest. Only a
complete listing under comparable scope, approval and source identity establishes
removal; `collection=removed` pages those exact historical records. Unknown
current inventory returns `not_established`; complete empty inventory returns a
committed zero-count manifest. Every read rechecks grants. Full records are paged
without text clipping (100 entities/200 evidence per ingestion page, 256 KiB page
budget, 64 KiB per record). Large discovery is separate from bounded legacy
curation/publication; estate-scale semantic projection remains unqualified.

Readers can discover current scope handles without knowing hashes in advance,
then select 1–20 exact entity IDs using a current manifest/digest. Selection resolves
all referenced evidence from that manifest, including later ingestion pages; a
changed manifest, grant or policy refuses. See [exact inventory consumption](staged-inventory-consumption.md)
for schema, budgets, currentness and authority limits. These reader operations
remain available when source management is disabled.

## Local measurement and qualification

`scripts/measure_runtime.py` measures synthetic lexical metadata lookups with
explicit source grants and no models/services. Its report declares exact scope,
request count, environment, warm timings, process RSS and excluded components.
It is not a cloud price, installation footprint or customer latency guarantee.

`scripts/drill_semantic_planning.py` and `scripts/drill_inventory.py` accept only
a protected URL to a disposable loopback `knowledge_build_*` database. They
qualify restricted-source readbacks and registry transactions independently.
Public release, vendor/native ACL qualification and a seven-day soak retain
separate gates in the [completion plan](platform-completion-plan-2026-10-03.md).
