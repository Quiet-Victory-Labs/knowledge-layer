# WeKnora and recent knowledge repositories

Research date: **October 11, 2026, America/Chicago**. Activity window: **June 11–October 11, 2026**, through the observed capture time. The local June 11 boundary is `2026-06-11T05:00:00Z`; the main API collection began `2026-10-12T00:01Z`, still October 11 in Chicago. Release dates in the tables are UTC unless stated otherwise.

The strongest adaptations for Knowledge Layer are clearer evidence search, a direct citation reader, comparison of proposed and current knowledge, and a visible path from agent connection to reviewed publication. These fit the existing registry, source grants, snapshot-bound citations and human review. The first search improvement is implemented in this branch; the other items below are proposals.

## How the shortlist was selected

GitHub repository searches were sorted by current stars for active, non-fork projects in knowledge RAG, knowledge graphs and semantic layers. A separate search covered knowledge-agent projects created in the four-month window. Twelve relevant projects were selected for product and implementation fit. This is a curated shortlist, not a complete global ranking or a historical star-growth leaderboard.

Stars are dated discovery signals. They do not establish accuracy, adoption, operating readiness or superiority. Recent push timestamps were cross-checked against releases or default-branch commits. Established capabilities and additions during the requested window are distinguished. Search results that pointed to a SAG fork were resolved to the canonical **Zleap-AI/SAG** repository.

Primary evidence includes GitHub metadata, release notes, official documentation source and selected implementation files. We did not deploy competitors, call their models or run customer workloads. WeKnora's 29 isolated graph/trace helper tests passed; that verifies those helpers only. The wider competitor findings are source/documentation inspection, not comparative performance measurements.

## WeKnora: what to adapt

[Tencent/WeKnora](https://github.com/Tencent/WeKnora) has **33,215 stars** in the saved October 11 snapshot, was created July 22, 2025, and was inspected at [`00474f734d6c`](https://github.com/Tencent/WeKnora/tree/00474f734d6c7c0911d7485465d7c7286d0047cb). Its product combines document retrieval, agent work and maintained Wiki pages. The latest published release is **v0.8.2, September 24**; subsequent October main changes are identified separately.

The window contains six releases and 1,395 main-history records, including merges. Commit count is not a count of shipped features.

| Release | Date | Relevant additions documented by the project |
| --- | --- | --- |
| [v0.6.3](https://github.com/Tencent/WeKnora/releases/tag/v0.6.3) | June 26 | Citation previews, retrieval progress, Wiki folders, MCP OAuth. |
| [v0.7.0](https://github.com/Tencent/WeKnora/releases/tag/v0.7.0) | July 17 | Scoped keys, principals, runtime queues, worker controls. |
| [v0.7.1](https://github.com/Tencent/WeKnora/releases/tag/v0.7.1) | July 24 | Knowledge-base activity and platform keys. |
| [v0.7.2](https://github.com/Tencent/WeKnora/releases/tag/v0.7.2) | August 7 | Folder trees, editable chunks, Wiki revision comparison. |
| [v0.8.0](https://github.com/Tencent/WeKnora/releases/tag/v0.8.0) | September 3 | Persistent skill sandbox, memory controls, Office parsing. |
| [v0.8.2](https://github.com/Tencent/WeKnora/releases/tag/v0.8.2) | September 24 | Scoped MCP endpoints, browser integration, conversation control. |

Wiki generation already existed before June 11; revisions and reviewability improved during the window. Knowledge Layer already has durable agent builds, source evidence, human acceptance, separate publication and an authorized graph. A new Wiki experience should present that reviewed state through readable pages and exports.

| Pattern | Source evidence | Adaptation and acceptance condition |
| --- | --- | --- |
| Natural-question search with transparent fallback | [v0.8.2 retrieval tool](https://github.com/Tencent/WeKnora/blob/3e8b0bfc80b845b2d4b2ed683994748741450a97/internal/agent/tools/search_knowledge.go) | Expose the requested and actual search behavior. A partial candidate reports missing terms and retains its exact citation. Implemented here as deterministic lexical matching. |
| Click a citation to inspect its evidence | [Citation popover lifecycle](https://github.com/Tencent/WeKnora/blob/00474f734d6c7c0911d7485465d7c7286d0047cb/frontend/src/composables/useCitationPopover.ts), introduced [June 15](https://github.com/Tencent/WeKnora/commit/05f39b859f84693fbb7326b65271a31976ad2de9) | Show a cross-source cited passage, revision, locator and availability without leaving Review. Cache by authorized scope and exact evidence identity; changed pins or lost grants must invalidate cached content. |
| Compare knowledge revisions | [Revision diff](https://github.com/Tencent/WeKnora/blob/00474f734d6c7c0911d7485465d7c7286d0047cb/frontend/src/utils/wikiRevisionDiff.ts), history added [July 27](https://github.com/Tencent/WeKnora/commit/80a5003cc99a427098afe184eee6601916d3d156) | Compare current accepted and proposed assertions with their evidence. An explicit retire/replace decision uses current pins; missing historical evidence remains unknown. |
| Visible background progress | [Runtime queues](https://github.com/Tencent/WeKnora/blob/00474f734d6c7c0911d7485465d7c7286d0047cb/frontend/src/views/system/RuntimeQueues.vue), released in v0.7.0 | Extend existing BuildProgress with stage receipts and useful blockers. Evidence for completed work must come from actual job state. |
| Task-focused graph navigation | [Graph focus/layout](https://github.com/Tencent/WeKnora/blob/00474f734d6c7c0911d7485465d7c7286d0047cb/frontend/src/views/knowledge/wiki/wikiGraphLayout.ts), October main work | Add deterministic source/topic focus to the current map. Layout and proximity carry no semantic approval authority. |

WeKnora's own code is MIT in its [actual license](https://github.com/Tencent/WeKnora/blob/00474f734d6c7c0911d7485465d7c7286d0047cb/LICENSE); GitHub's `NOASSERTION` metadata alone was insufficient. Its bundled dependency notices have separate terms. This branch imports no competitor code or assets.

## Other relevant projects

The pattern column describes current source/documented behavior. The dated activity proves work in the requested window; it does not imply every listed pattern was introduced in that release.

| Project | Stars captured Oct 11 | Dated activity in window | Useful pattern and Knowledge Layer fit |
| --- | ---: | --- | --- |
| [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) | 125,556 | [v0.9.86](https://github.com/Graphify-Labs/graphify/releases/tag/v0.9.86) · 2026-10-11 | Deterministic structure and inspectable relationship paths. Use explicit source-to-concept paths and a task-focused graph view. |
| [infiniflow/ragflow](https://github.com/infiniflow/ragflow) | 92,003 | [v1.0.0-rc1](https://github.com/infiniflow/ragflow/releases/tag/v1.0.0-rc1) · 2026-09-29 | Retrieval testing, visible candidate relevance and citation previews. Expose how evidence was found before improving model-based retrieval. |
| [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) | 40,068 | [v1.5.7](https://github.com/HKUDS/LightRAG/releases/tag/v1.5.7) · 2026-09-02 | Graph/context retrieval with a separate query-only workspace. Keep consumer context use simple while steward tools remain available. |
| [volcengine/OpenViking](https://github.com/volcengine/OpenViking) | 39,677 | [v0.5.0](https://github.com/volcengine/OpenViking/releases/tag/v0.5.0) · 2026-10-09 | Named resource scopes, intent analysis and optional reranking. Offer explicit search scope and progressive evidence reads. |
| [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex) | 39,051 | [v0.2.22](https://github.com/VectifyAI/PageIndex/releases/tag/v0.2.22) · 2026-10-08 | Document section trees with traceable page references. Preserve section/page structure in evidence navigation. |
| [getzep/graphiti](https://github.com/getzep/graphiti) | 31,652 | [v0.30.2](https://github.com/getzep/graphiti/releases/tag/v0.30.2) · 2026-09-08 | Temporal facts, source provenance and hybrid retrieval. Explain changed support and prior/current meaning in maintenance views. |
| [trustgraph-ai/trustgraph](https://github.com/trustgraph-ai/trustgraph) | 2,792 | [v2.8.17](https://github.com/trustgraph-ai/trustgraph/releases/tag/v2.8.17) · 2026-09-08 | Ontology-led extraction, interoperable knowledge and hybrid document retrieval. Make reviewed semantics and evidence visible as a reusable product. |
| [Zleap-AI/SAG](https://github.com/Zleap-AI/SAG) | 2,569 | [fnos-v1.5.11-fnos](https://github.com/Zleap-AI/SAG/releases/tag/fnos-v1.5.11-fnos) · 2026-09-30 | Event/entity indexing with source tracing and MCP/CLI access. Evaluate relational retrieval on our fixed corpus before adopting it. |
| [aakarim/OpenLore](https://github.com/aakarim/OpenLore) · new in window | 1,129 | [v0.7.3](https://github.com/aakarim/OpenLore/releases/tag/v0.7.3) · 2026-10-01 | Agent-friendly shared context with governed file writes. Make published context portable and easy to retrieve in existing agents. |
| [yuezhiai/jonex](https://github.com/yuezhiai/jonex) · new in window | 1,346 | [main commit](https://github.com/yuezhiai/jonex/commit/5571db5c682fb3182e968d2e9d34ef415a2638c3) · 2026-10-10 | Ontology-first knowledge compilation and source references. Study structured extraction and feedback UX as proposal inputs. |
| [echopath-labs/openDomain](https://github.com/echopath-labs/openDomain) · new in window | 303 | [v0.1.1](https://github.com/echopath-labs/openDomain/releases/tag/v0.1.1) · 2026-09-16 | Git-readable domain context and explicit Candidate review. Show accepted knowledge, proposed changes and evidence side by side. |

Three of these projects were created within the requested window: **OpenLore** (June 25), **openDomain** (July 7) and **Jonex** (July 19). Their smaller communities make implementation maturity and qualification especially important to inspect directly.

## Implementation findings that change the plan

- **RAGFlow** provides a [retrieval testing surface](https://github.com/infiniflow/ragflow/blob/b45dfd927f7bad20069558e3563adbbacab0140d/docs/guides/dataset/retrieval_testing.md) with thresholds, keyword/vector weighting and candidate inspection. Its September 29 release is a Go rewrite **release candidate**. The useful near-term transfer is visible retrieval diagnostics and a small repeatable corpus; a language/runtime migration has no established value for Knowledge Layer.
- **LightRAG** [v1.5.7](https://github.com/HKUDS/LightRAG/releases/tag/v1.5.7) adds a query-only, mobile-friendly `/workspace` with configurable welcome and empty states. Its [retrieval implementation](https://github.com/HKUDS/LightRAG/blob/453dce83d6d0354a06e46c8d4029a0895c4e054b/lightrag/operate.py) preserves source references while composing context. Knowledge Layer can give consumers a short path to published context and keep provenance inspectable.
- **PageIndex** [v0.2.22](https://github.com/VectifyAI/PageIndex/releases/tag/v0.2.22) separates deterministic PDF structure building from model-written summaries and reasoning. It supports local operation, but its reasoning path still needs a configured model. The transferable part is document hierarchy and exact page references; neither vectorless indexing nor a published benchmark establishes our result quality.
- **OpenViking's current retrieval docs** describe [global vector recall, optional intent analysis and optional reranking](https://github.com/volcengine/OpenViking/blob/4c9a755bff4d31fd9bd9a3be1a19a6054ef3cea5/docs/en/concepts/07-retrieval.md). Its named filesystem scopes help explain retrieval, but a directory-shaped product description is not evidence of a particular traversal algorithm. Its latest Gateway is beta and changes the model-request path; our existing external agent handoff remains the suitable integration seam.
- **Graphiti** [v0.30.2](https://github.com/getzep/graphiti/releases/tag/v0.30.2) includes group isolation and retrieval fixes. Its temporal fact model suggests a clear current/prior-support view. Knowledge Layer already invalidates changed evidence; the missing product piece is a readable impact inbox and comparison, not another automatic approval system.
- **TrustGraph** [v2.8.17](https://github.com/trustgraph-ai/trustgraph/releases/tag/v2.8.17) expands hybrid document retrieval, auditing and its extensible UI. Its ontology-first framing is directly relevant to our typed semantic contracts and RDF provenance. These are documented release capabilities, not vendor account validation performed here.
- **OpenLore** [v0.7.3](https://github.com/aakarim/OpenLore/releases/tag/v0.7.3) focuses on governed retrieval/write reliability and portable release artifacts. **openDomain** [v0.1.1](https://github.com/echopath-labs/openDomain/releases/tag/v0.1.1) improves native grounding requests while keeping Candidate review explicit. Both support a small, inspectable context product that works with agents people already use.
- **Jonex** documents an ontology-first, multimodal knowledge pipeline. Its [license](https://github.com/yuezhiai/jonex/blob/5571db5c682fb3182e968d2e9d34ef415a2638c3/LICENSE) is amended Apache 2.0 with supplementary terms; **OpenViking** declares AGPL-3.0. We studied their behavior and copied no code. **SAG** claims benchmark-leading event/entity retrieval; we have not reproduced that claim, so adoption requires a fixed, permission-aware local comparison.

## What was built in this branch

The shared document-evidence search now recognizes complete leading conversational phrases such as “Please tell me about …”. It preserves the default all-meaningful-term search behavior and introduces an explicit `match_mode="ranked"` option.

Ranked mode uses partial candidates only when the complete authorized scan finds no full match. It requires at least three meaningful query terms, at least two matching terms and two-thirds coverage. Quoted terms, recognized constraint/unit tokens, numbers and identifiers remain mandatory. Each row reports `match_type`, `matched_terms` and `unmatched_terms`; the response distinguishes `lexical_all_query_terms` from `lexical_ranked_partial_terms`.

This is bounded lexical candidate retrieval. It performs no embeddings, synonym expansion, unit conversion, logical interpretation or generated answering. Returned evidence is a drafting surface, independent of approval/publication. Search scans authorized stored evidence and preserves exact snapshot/evidence references, source revisions, locators and retention/coverage metadata. A failed source refresh can leave the last saved evidence snapshot available; a search result does not certify current upstream truth.

Python SDK, compact MCP, advanced MCP and HTTP expose the same explicit mode. HTTP and MCP document responses use a complete JSON byte budget so term diagnostics are not silently clipped. Source-metadata search retains its existing behavior. No new service, database, model account or dependency was added.

Focused validation covers natural questions against retained passages, full-match priority, opt-in partial matches, meaningful-term coverage, generic identity noise, constraint preservation, smart quotes, source grants, snapshot changes and complete transport diagnostics. No measured model uplift or broad RAG superiority is claimed.

## Next additions, ordered by value

1. **Direct pinned evidence reader:** turn identifier-heavy cross-source citations into an inspectable source passage with current availability. Extend `ReviewBatch`/`reviewEvidence` and the existing citation resolver; require changed-pin and lost-grant tests.
2. **Current/proposed comparison and maintenance inbox:** present existing drift/stale-review packets with explicit steward actions. Use `get_maintenance_work` and existing exact acceptance/publication gates; keep hidden dependencies opaque.
3. **Observed agent setup milestone:** record a challenge-bound successful authenticated handoff and show the next step. Copying configuration alone remains unverified; a transport 200 alone is insufficient.
4. **Readable published knowledge pages:** render/export the existing accepted registry state with provenance and revalidation. Current company-guidance Markdown export already exists; a wider semantic-page export is an additional scope, not a new authority.
5. **Visible fixed-corpus evaluation:** show candidate relevance, citation correctness and permission/freshness cases separately. Compare optional retrieval strategies on the same pinned sources and budgets before making quality claims.

## Source snapshots

These pins identify the observed default-branch state within the activity window. Release tags above identify shipped versions separately. Snapshot inspection does not establish deployability or correctness of every path.

- [Graphify-Labs/graphify: source snapshot](https://github.com/Graphify-Labs/graphify/tree/a5ba60ddf6293d9f380321965ecf1084391dcc2b) · `a5ba60ddf629`. Created 2026-04-03.
- [infiniflow/ragflow: source snapshot](https://github.com/infiniflow/ragflow/tree/b45dfd927f7bad20069558e3563adbbacab0140d) · `b45dfd927f7b`. Created 2023-12-12.
- [HKUDS/LightRAG: source snapshot](https://github.com/HKUDS/LightRAG/tree/453dce83d6d0354a06e46c8d4029a0895c4e054b) · `453dce83d6d0`. Created 2024-10-02.
- [volcengine/OpenViking: source snapshot](https://github.com/volcengine/OpenViking/tree/4c9a755bff4d31fd9bd9a3be1a19a6054ef3cea5) · `4c9a755bff4d`. Created 2026-01-05.
- [VectifyAI/PageIndex: source snapshot](https://github.com/VectifyAI/PageIndex/tree/1a060dfc5c512ece03aeaa9cb80d76dbb5358bdf) · `1a060dfc5c51`. Created 2025-04-01.
- [getzep/graphiti: source snapshot](https://github.com/getzep/graphiti/tree/0a3b1711b018e5590cf5c44a3ee3e67d6733ee13) · `0a3b1711b018`. Created 2024-08-08.
- [trustgraph-ai/trustgraph: source snapshot](https://github.com/trustgraph-ai/trustgraph/tree/ea19308aaeb8458b9d988794eff7d4c0016bd43f) · `ea19308aaeb8`. Created 2024-07-10.
- [Zleap-AI/SAG: source snapshot](https://github.com/Zleap-AI/SAG/tree/56f073c1c0c300b46707c7249d48732730656cb0) · `56f073c1c0c3`. Created 2025-11-07.
- [aakarim/OpenLore: source snapshot](https://github.com/aakarim/OpenLore/tree/9a8614ba5d2f8b91ebe9bd590268697e5ef1d0bd) · `9a8614ba5d2f`. Created 2026-06-25.
- [yuezhiai/jonex: source snapshot](https://github.com/yuezhiai/jonex/tree/5571db5c682fb3182e968d2e9d34ef415a2638c3) · `5571db5c682f`. Created 2026-07-19.
- [echopath-labs/openDomain: source snapshot](https://github.com/echopath-labs/openDomain/tree/f8e09c70ff81f3f2af395ddaf70d8b0b6261c802) · `f8e09c70ff81`. Created 2026-07-07.
