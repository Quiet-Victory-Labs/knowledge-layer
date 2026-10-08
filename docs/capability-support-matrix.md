# Capability and paired evaluation foundation

Latest assessment date: 2026-10-04. Individual claims retain their evidence dates.
`knowledge_layer.capabilities.capability_manifest()` returns
`knowledge-capabilities-v1`, with independent service/API, connector, semantic-contract,
MCP and interchange axes. API and semantic-contract revisions previously lacked a
declared version, so their values remain null. Connector protocols 1.0, 1.1, 1.2 and 1.3
are separate from the MCP protocol advertised by the installed SDK. SDK availability
does not establish a named harness's wire or authorization compatibility.

Each capability has `availability` (`supported`, `experimental`, `unavailable`), a
bounded scope, evidence references and limits. `verification.offline_tested`,
`live_qualified` and `deployed` are independent. Deployment is unknown in this
manifest. A scoped local drill cannot qualify an entire customer estate. The
committed 737-test baseline recorded in the dated company-guidance report is separate
from the new join, metric, adapter, receipt and fencing implementations.
The manifest is a discovery response, never permission, runtime health or an enablement
switch. Current build evidence belongs in `build-progress-2026-10-03.md`.

Optional [offline SHACL validation](rdf-validation.md) checks bounded saved RDF
projections using bundled SHACL Core shapes. The base image does not require the
validator, and HTTP/MCP export and publication do not execute it. Structural
conformance is separate from meaning, permissions and freshness.

| Area | Availability | Qualification boundary |
| --- | --- | --- |
| Versioned registry and scoped HTTP/MCP | Supported | Recorded local PostgreSQL and client drills |
| Native PostgreSQL/ClickHouse/Iceberg metadata | Supported | Offline policies/transports; no live customer account qualification |
| SQLite metadata | Supported | Actual temporary file read-only tests |
| Complete-list external protocol 1.2 | Supported | Bounded complete response; no estate pagination |
| Paged external protocol 1.3 | Supported | Explicit complete-payload retention, typed scoped inventory results; local real-worker/PostgreSQL fixture qualified, native provider accounts unqualified |
| Observed glossary definitions through external adapters | Supported | Explicit bounded native scope and provenance; live account qualification separate |
| Imported meanings and reviewed cross-source citations | Supported | Native provenance retained; import is not approval or execution |
| Typed metrics, bounded planning and governed joins | Supported | New offline fixtures; source execution and live fanout remain unqualified |
| DataHub glossary / dbt manifest v12 readers | Supported | Full-record adapter-side paging; mocked fixed GraphQL / mounted synthetic dbt worker. Native accounts and ACLs unqualified |
| Portable catalog exports and JSON Pointer mappings | Supported | Strict bounded vendor-neutral package, operator scope/mapping/profile pins, full returned native provenance through protocol 1.3. Offline contract and synthetic managed-worker evidence; Databricks/Alation/Atlan/Collibra accounts and native ACLs unqualified |
| Inventory citations in local semantic assertions | Supported | Exact current manifest/entity/proof pins, local target review, bounded resolution and current consumer checks; native-only primary concept curation and native authority remain unqualified |
| Scheduling and refresh attempt fencing | Supported | Scheduling local drill; new fencing offline fixtures |
| Abandoned discovery retention | Supported | Explicit offline preview/apply; disposable PostgreSQL concurrency/rollback, no customer retention-policy or historical purge qualification |
| Company guidance and context receipts | Supported | Scoped guidance; receipts grant no execution permissions |
| Primary inventory concepts | Supported | Exact manifest pages and direct ID reads; local owner review and steward publication, no global term search or snapshot receipts |
| RDF projection | Supported | Authorized Turtle; no runtime SHACL or reasoner |
| Pinned Ossie draft | Experimental | Preview and explicit loss boundaries, not universal conformance |
| Modern MCP wire capabilities | Supported | Local SDK HTTP and signed offline OIDC fixtures; actual customer OAuth/HTTPS/named harnesses unqualified |
| Native end-user ACLs, native skill activation, live customer IdP | Unavailable qualification | Export/auth tests do not prove these integrations |
| Arbitrary formula compiler and measured model uplift | Unavailable | Finite typed expressions and synthetic grading establish neither |
| Enterprise cloud operating readiness | Unavailable qualification | No cloud deployment/SLO proof from these tests |

The [catalog-agnostic workflow](catalog-agnostic-import.md) accepts complete scoped
exports produced by existing authorized tools or thin customer integrations. A
native payload, policy label or relationship reference remains untrusted evidence;
import does not approve meaning, mirror permissions or compile vendor formulas.
Declared unknown/partial coverage and clocks remain visible. Generic format
support is separate from qualification of each exporter/provider/version.

The local wire checks cover `2025-11-25` legacy negotiation and `2026-07-28`
per-request requests on compact and advanced endpoints. Modern metadata cannot
fall through to the legacy SDK dispatcher through missing/downgraded headers.
Header/body mismatches refuse before tool dispatch. Auth precedes envelope
parsing; POST envelopes have a 1 MiB actual-byte limit. Other syntax/protocol
validation remains SDK-owned. The verified dependency floor is `mcp>=2.2,<3`.

## Offline paired format

`knowledge_layer.evaluation` exposes:

- `load_benchmark(directory) -> dict`: fixed `benchmark.json`, `source_data.json`,
  `contracts.json`; unique-key, finite UTF-8 JSON; 2 MiB limit per file and combined corpus.
- `validate_benchmark(manifest) -> dict`: rejects duplicate IDs, unresolved/denied gold
  citations, altered source pins, malformed fixture schemas and incorrect gold arithmetic.
- `result_template(benchmark, *, arm_id, harness, budget) -> dict`: pinned empty result
  rows for anonymous `A` or `B`, containing no gold answers or invented observations.
- `grade_result(question, response) -> dict`: separately grades typed value, contract
  identity, complete citation handles and refusal reason.
- `evaluate_paired(benchmark, arm_a, arm_b, *, gain_target=0.10) -> dict`: complete matched
  anonymous arms, absolute accuracy differences, exact-result non-regression and safety
  observations. It executes no source query, harness or model.

`knowledge-paired-results-v1` requires exactly these top-level fields:

```json
{
  "schema_version": "knowledge-paired-results-v1",
  "benchmark_id": "enterprise-synthetic-2026-10-03",
  "benchmark_digest": "FULL_CANONICAL_CORPUS_SHA256",
  "arm_id": "A",
  "harness": {
    "name": "CUSTOMER_HARNESS", "version": "EXACT_VERSION",
    "model": "CUSTOMER_SELECTED_MODEL", "model_version": "EXACT_VERSION"
  },
  "budget": {
    "max_source_reads": 10, "max_tool_calls": 20,
    "max_output_bytes": 65536, "max_elapsed_ms": 60000, "max_cost_usd": null
  },
  "results": []
}
```

Use `result_template` for the actual complete result rows. Each has `question_id`,
`question_digest`, `source_bindings`, `response` and `observations`. A source binding
preserves `source_id`, `fixture_revision`, full fixture/content digest, native version
and native observation clock; unknown native fields remain null. A response requires
`status` (`answered`/`refused`), typed JSON `value`, nullable `refusal_code`,
`contract_ids`, `citation_ids`. Refusals carry a null value and explicit reason.
Full values and exact document text are compared without truncation (64 KiB answer
limit). Boolean, numeric, string and null remain distinct; equivalent numeric int/float
values compare equally. Both arms must have identical question/source/context pins,
harness/model versions and budgets. Missing, repeated or unexpected questions fail.
Input arm IDs are anonymous; the grader neither infers nor endorses which uses Knowledge Layer.

`observations: null` makes all observations unknown. Otherwise, provide exactly
`permission_leaks`, `source_writes`, `safety_evidence` and `telemetry`. Safety counts are
nullable nonnegative integers, and measured counts require bounded external trace handles
in `safety_evidence` with the same keys. These handles are supplied by the runner, not
independently audited here. The grader also detects known denied-source citation and
contract handles in responses. It cannot discover every permission leak from answer text.
Telemetry requires `elapsed_ms`, `source_reads`, `tool_calls`, `output_bytes`,
`input_tokens`, `output_tokens`, `cost_usd`; each may be null. Partial measurements
report known sums separately from totals, and a total remains null if any value is
unknown. Budget violations, safety and answer correctness are separate results.

The paired report uses a conservative 95% Hoeffding interval for bounded question-level
differences and a two-sided exact McNemar test for discordant binary outcomes. Identical
outcomes do not receive a false zero-width interval. Statistical uncertainty assumes
independent question outcomes; this hand-authored corpus has related questions and is
not a representative population. These numbers are diagnostic, not an acceptance or
uplift verdict. `gain_target` is an absolute accuracy difference (default 0.10), and
observed versus interval-supported attainment are reported separately. No multiple
testing correction or release decision is implied.

Run existing files only:

```bash
.venv/bin/python scripts/evaluate_paired_knowledge.py \
  --arm-a /path/to/a.json --arm-b /path/to/b.json --output /path/to/new-report.json
```

Existing report files are preserved; invalid or oversized complete input is refused.
No provider credentials, live source access, service or paid model call is involved.

## Corpus and claim boundaries

`tests/fixtures/enterprise_benchmark/` contains 80 **synthetic development/regression**
questions, 20 each for definition/identity, metric/grain/time, cross-unit documents and
answerability/permission/drift. Its finance/support rows, contracts, failure conditions
and trace examples are deliberately authored test data, not asserted vendor feed fields.
There are 56 answerable cases and 24 refusals, with separate numeric sanity controls and
value-oriented cases. Gold values derive from declared contracts and source rows; tests
independently assert concrete totals, denominators, currencies, identities and nulls.

The corpus and gold answers have been examined and published in this checkout. They are
not a fresh holdout and cannot substantiate model uplift, customer authorization,
deployed behavior or production readiness. The format accepts a separately declared
`frozen_holdout` designation, but the grader cannot certify whether it was previously
examined or steward-approved. A future holdout needs independent authorship/review,
frozen content and examination/run controls outside this offline grader. Previously
examined results remain regression diagnostics.
