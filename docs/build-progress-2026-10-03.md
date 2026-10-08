# Platform implementation and qualification, 2026-10-03

This implements the [completion plan](platform-completion-plan-2026-10-03.md).
It is development evidence for a single-organization alpha. Source metadata,
review, execution, provider permissions, deployment and distribution remain
separate claims.

## Implemented

- Current inventory scopes and exact selected entities/evidence are consumable
  over authenticated HTTP and both MCP surfaces, with pinned manifest digests,
  whole-response budgets and final current grant/generation rechecks.
  PostgreSQL readers explicitly use READ COMMITTED even when an operator's pool
  defaults to repeatable read; actual revoked grants and successor commits during
  response assembly refuse. Discovery remains separate from reviewed semantics.
- Adapter contributors can check response files offline through the same
  normalizer as online discovery. The credential-free CLI reports bounded wire
  counts/digests, preserves unknown document coverage and distinguishes legacy
  implicit completeness from explicit declarations. This is not provider ACL
  or complete-account qualification.
- Offline operators can remove already-aborted, unreferenced discovery rows
  using exact previews, explicit cutoffs and row budgets. Atomic audits and
  source/job locks preserve active work, committed history and source clocks.
  Output reservation precedes any mutation; post-commit output failures retain
  actual deletion status. See [retention boundaries](inventory-retention.md).
- Reviewed delegation permits an exact authenticated service identity to publish
  unchanged reviewed metadata after successful refresh. It is disabled by
  default and requires OIDC, a direct source manage grant, explicit operator
  subject approval, a separately approved immutable policy, exact meaning/review
  bindings, expiry and publication budget. The source kill switch withdraws
  delegated publications across prior policy revisions while preserving human
  publications and audit history. Novel meaning and cross-source dependencies
  still require review. See [operator workflow](autonomy-policies.md).
- Typed, evidence-reviewed cross-source joins pin both source assets, revisions,
  exact fields, identifier namespaces, directional cardinality and citations.
  Revocation, withdrawn publication and changed metadata block effective use.
  Coverage remains unmeasured until independent authorized source reads establish
  it; planning refuses fanout without a result-grain policy. No fuzzy identity
  matching or cross-engine result execution was added.
- Metric v2 adds dimensions, typed filters, time/grain/timezone, cohort, units,
  additivity, finite arithmetic and paired ratios. Legacy definitions retain
  their format. Parameterized PostgreSQL, ClickHouse and SQLite plans load
  current approved bindings from the registry and expose schema, grain,
  precision, scan, result-limit and missing/zero checks. Plans stay unexecuted.
  Unsupported interchange mappings are reported instead of dropping semantics.
- Principal-bound task receipts pin selected source/semantic state, operator
  policy and current company guidance. Signatures prevent caller edits; five
  minute expiry forces renewed context. The process-local default invalidates
  across restart and requires the same process; optional [operator keys](context-receipt-keys.md)
  support explicitly configured replicas of one registry and rolling rotation.
  First-use default key initialization is serialized. Revocation and
  effective dependency changes invalidate receipts. These receipts grant no
  source permissions and do not prove that a harness obeyed instructions.
- Refresh workers carry claim generations through subprocess execution,
  heartbeat, completion and registry mutation. Source/job locks guard commits;
  expired, cancelled or superseded children cannot publish discovery snapshots
  or advance clocks. Job attribution records the exact snapshot committed by
  its generation; retry clears previous attribution.
- Authorized maintenance polling returns bounded, version-bound interpretation
  packets and dependency impact for the customer's harness. Discovery,
  interpretation, review, publication and consumer freshness remain separate.
  Capped scans report lower-bound counts and unknown absence, preserving reader
  permissions and manager-only schedule details. It does not automate approval.
- Paged discovery staging persists full entity/evidence rows under owner, source,
  scope, policy and watermark pins. Exact replay, terminal/count/digest checks,
  generation CAS, revocation and final worker fencing protect the atomic head.
  Comparable completed listings page explicit removals; partial/unknown listings
  cannot establish deletion. API/MCP reads avoid aggregate legacy projections.
- A versioned capability manifest distinguishes availability, offline tests,
  live qualification and deployment. The paired evaluator includes 80 public
  synthetic finance/support/identity cases with independently derived gold,
  source/version/harness/budget pins, citation/refusal checks and unknown safety
  receipts. It is a development corpus, never a fresh held-out value claim.
- Optional dbt manifest v12 import and an explicit-URN DataHub glossary reader
  preserve native semantic records as upstream evidence. Unknown schemas,
  missing selected records, denied reads, partial responses and oversized
  definitions refuse. Native approval never grants local authority or SQL
  execution. The [sample adapter](../examples/native_semantic_adapter.py)
  connects these helpers through the existing operator-scoped v1.2 protocol.
- Distribution verification inspects wheel/source contents, installs both the
  base wheel and source archive in an isolated runtime outside the repository,
  and runs the packaged enterprise consumer example from the source archive. It produces
  checksums and an unsigned local receipt. CI now includes the disposable
  Keycloak integration drill; image builds include LICENSE and NOTICE.
- Compact/advanced HTTP MCP transports cover legacy negotiation and modern
  `2026-07-28` per-request metadata. An authenticated 1 MiB envelope guard closes
  the SDK's header-only routing bypass; missing/downgraded mirrored protocol
  headers cannot execute a modern body through legacy handling. The dependency
  floor matches the installed/tested MCP 2.2 APIs. Signed OIDC transport fixtures
  use an offline test key and establish no customer IdP or live OAuth claim.

## Current frozen increment qualification

Clean detached `d9b8c06` collected and passed **1,703 tests** in 74.63 seconds,
with one existing SQLite expression-index reflection warning. Ruff, the
**310-file** secret gate and diff checks passed. The secret gate now checks a
private temporary copy of the reviewed baseline, accepting known candidate
location changes without modifying the tracked baseline or hiding new secrets.

The guarded disposable PostgreSQL 18.6 drill passed **49 inventory checks** and
**16 delegation/recovery checks** from that exact clean tree. It exercised
current scope/selection retrieval with real server cursors, grant withdrawal
and successor commits during response assembly even under a repeatable-read
pool default. A development negative control that removed the read isolation
fix reproduced stale selection; restoring the fix refused it.

Retention checks read back the exact 12 deleted aborted-inventory rows and one
audit event, prove audit-failure rollback and concurrent apply winner/refusal,
preserve other registry totals/current heads/history/clocks, and refuse an old
preview. Actual PostgreSQL CLI preview matches the library through a new private
file; an empty CLI apply performs no writes. The source fixture retained three
rows with separate known/zero/missing states under a SELECT-only role. All
owned registry rows, source fixtures and the disposable database/container were
removed; all 23 registry table totals returned to their starting values.

Unsigned wheel and source archives from `d9b8c06` passed base-only isolated
installation, exact inventory selection, actual local retention deletion/audit,
the contributor adapter command, installed retention CLI help and the packaged
enterprise consumer example outside the checkout. Private local checksums and
receipts are in `.local/inventory-selection/dist-d9b8c06/`; PostgreSQL readbacks
are in `.local/inventory-selection/postgres-clean-d9b8c06.json`. These qualify
this implementation on the current runner, not published artifacts, customer
provider permissions, native paid-model clients or a cloud deployment.

The preceding clean `2b26192` checkpoint passed **1,610 tests**, its wheel/source
checks and **41 inventory / 16 delegation** PostgreSQL checks. The current
checkpoint adds bounded abandoned-inventory retention and stronger installed
distribution assertions. The earlier qualification boundaries below remain
separate evidence for their recorded source revisions.

## Earlier evidence

The clean detached implementation commit `22cbca0` collected and passed **1,074
tests**, including **49 MCP transport cases**, in 24.32 seconds. One known SQLite
expression-index reflection warning remains. Ruff, the 271-file secret scan,
the production UI image build and Helm lint/render passed. These checks establish
the tested implementation; they are not a public release or customer readiness
verdict. The final documentation commit records this frozen implementation.

`scripts/drill_semantic_planning.py` passed against disposable loopback
PostgreSQL 18.6 at schema v12. The drill created an owner-isolated fixture and
restricted read role, executed generated SQL in a read-only transaction,
reconciled two grouped rows, confirmed three selected/two known/one missing/one
observed-zero rows, checked no duplicate grain, confirmed insert privilege was
absent, and rejected an old claim after same-worker takeover. Fixture resources
were removed. No customer source was written.

Wheel/source archives built from that exact clean tree. Fresh base-only installs
of both initialized the registry and MCP server outside the checkout. The
enterprise example extracted from the source archive then passed using the
installed package in isolated Python mode. Checksums and the installation receipt
live in `.local/build-clean-22cbca0/verification.json` and `SHA256SUMS`. These are
local unsigned artifacts, not a published release, multi-architecture image or
cloud deployment.

The rebuilt `knowledge-layer:build-22cbca0` image actually ran on linux/arm64 as
UID/GID 10001 with a read-only root filesystem and a 256 MiB memory limit.
Readiness/UI returned 200; unauthenticated agent HTTP returned 401. Twenty modern
wire probes across compact/advanced endpoints passed, including correct calls,
lists, header mismatch/downgrade refusal, authorization and actual-body overflow.
Both installed legacy SDK sessions negotiated `2025-11-25` and called the
capability tool. Instantaneous container memory was 99.92 MiB, not a peak or
full-install measurement. The private image receipt records its digest and size.

The clean-tree disposable Keycloak 26.7.5/PostgreSQL v12 drill passed real browser
code/PKCE login, CSRF logout and MCP JWT checks. Wrong audience, missing scope,
tampered signature, fresh group removal and expiry were denied. An already
issued JWT retained its group claims until expiry; that observed limit is
explicit. The IdP used a temporary TLS certificate and synthetic dev realm;
the application path used ASGI. This is not a customer IdP, deployed HTTPS proxy
or native source ACL qualification. Both temporary drill containers were removed.

## Reviewed delegation qualification

The clean detached implementation `c445c3a` collected and passed **1,223 tests**
in 36.21 seconds, including 47 delegation behavior, 44 HTTP/MCP transport,
30 schema and 21 drill-safety cases. Ruff, the 282-file secret scan and Helm
lint/render passed. The existing SQLite expression-index reflection warning
remains. This is a clean implementation result, not an enterprise release.

The exact-tree managed PostgreSQL 18.6 drill passed **14 checks**: native
SELECT-only metadata refresh, concurrent idempotent publication, exact actor/
budget/run persistence, unchanged review and source clocks, exhausted budget,
queued refresh refusal, replacement-policy kill switch, an actual blocked grant
revocation race, and late-expiry rollback verified by all-table row fingerprints.
Populated backup/restore compared all **23 persisted tables** at v13 before
fixture cleanup. Source NUMERIC values were reconciled without coercion,
including known zero and missing values. Every registry table total returned
to baseline, and managed containers and temporary credentials were removed.
The registered CI job repeats this drill; no customer database was touched.

Wheel and source archive built from that commit and passed fresh base-only
installation outside the checkout. The packaged enterprise consumer example
passed in isolated Python mode. Required autonomy modules, migration and
operator/drill docs were checked inside the distributions. These are unsigned
local artifacts, not public publication or other-architecture qualification.
Private receipts and checksums live in `.local/autonomy-qualification/` and
`.local/autonomy-distributions-c445c3a/`.

The rebuilt `knowledge-layer:autonomy-c445c3a` image ran on linux/arm64 as
UID/GID 10001 with a read-only filesystem and 256 MiB limit. Eight checks passed:
readiness/UI, unauthorized agent refusal, compact/advanced MCP default surface,
disabled autonomy HTTP, non-OIDC opt-in boot refusal and resource/security
configuration. This is local image runtime proof; no customer OIDC or cloud
deployment claim. All five GitHub jobs passed on `c445c3a`, including the new
autonomy integration and existing disposable Keycloak/Compose checks.

## Remaining qualification boundaries

Complete paged inventory persistence is implemented separately from legacy
bounded semantic snapshots. A discovery inventory is not a curated estate.
Native row/document/field ACL propagation, large-estate semantic projection,
temporal/composite cross-engine joins, actual customer harness/IdP/provider
qualification, broader unattended semantic decision classes, a seven-day soak,
generic VM/cloud receipts and independently reviewed signed public artifacts
remain required before claiming the corresponding beta or enterprise tier.
The support manifest reports those limits rather than equating local tests with
customer readiness.

## Consume the implementation

Use MCP `get_capabilities`, `get_task_context`, `revalidate_task_context`,
`plan_metric_query` and `plan_relationship`. The same consumer operations are
available under the authenticated agent HTTP API. A harness resolves context,
selects an approved assertion, revalidates, inspects the plan's required checks,
then uses its own authorized source tool for read-only execution and result
verification. Steward review and publication retain their existing boundaries.

DataHub and dbt helpers are independently bounded adapter components. See the
[adapter guide](native-semantic-adapters.md) for their precise scope.

## Earlier development qualification and scoped measurement

The working-tree broad run collected and passed **1,025 tests**, with one known
SQLite expression-index reflection warning. Ruff, the UI production build, Helm
lint/render, the synthetic MCP consumer and PostgreSQL v12 migration/readback
drills passed. These development results precede the clean-checkout freeze.

The paged inventory drill passed **29/29 checks** on disposable PostgreSQL,
including real simultaneous commit winner/loser behavior, exact durable pages,
complete empty/removal semantics, denied reads/mutations and source INSERT
refusal. Its final rerun verified zero owned metadata across legacy/inventory
tables and unchanged totals across all 20 registry tables. All 29 checks passed
again from clean `22cbca0`. The generated PostgreSQL metric plans and stale-worker
refusal passed again, with zero owned fixture metadata after cleanup. A v12 backup
restored into an isolated database with matching all-table row fingerprints and
schema. Four earlier task-owned legacy snapshots were faithfully restored rather
than hidden; the entire disposable qualification database/container and its
volume were subsequently removed. No customer database was targeted.

A separate local synthetic lexical measurement used 5 sources, 100 CSV tables,
1,000 fields, 20 warmup and 200 timed direct Python requests. Warm search p95 was
7.90 ms and scoped candidate context p95 was 49.51 ms; process lifetime peak RSS
was 125.94 MiB on macOS arm64/Python 3.14.4. This excludes PostgreSQL, HTTP/MCP
transport, published semantic authority, optional models and cloud deployment.
It is one bounded local measurement, not an enterprise SLO or full-install cost.

Run [the enterprise example](bounded-agent-consumption.md) for the reference
consumer workflow. It independently reads finance's known 20 USD and sales'
1,040 USD under different cohorts, leaves the overlapping total unknown and
refuses a receipt after publication withdrawal.

## Primary concepts over paged inventory — 2026-10-04

An inventory-only source now supports immutable local business concepts,
owner review, separate steward publication and complete direct/scoped reads.
It creates no legacy snapshot, whole-source readiness or executable native
formula. New proposals share existing quotas/archive/audit and source grants.
The [workflow](inventory-concepts.md) records currentness and transport boundaries;
the [dated qualification](inventory-concepts-qualification-2026-10-04.md) records
49 inventory, 15 paged-worker, 44 native/semantic and 16 autonomy/recovery checks
at its exact clean implementation baseline, plus isolated installed artifacts.
Global search and typed guidance/task receipt dependencies are the next consumer
wiring gap for these primary concepts. Native vendor/ACL and customer operating
qualification remain separate gates.
