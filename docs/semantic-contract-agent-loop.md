# Agent-built semantic contract

An external agent connects through the [bundled skill](../skills/knowledge-layer/SKILL.md) and protected MCP. The platform discovers bounded source metadata and optional document excerpts. The agent interprets evidence and proposes meaning. The platform validates source IDs, fields, citation handles, and current snapshot IDs before storing drafts. A human owner decides whether a draft applies. The shared contract and RDF projection keep approved meaning tied to its source version.

## What an agent can draft

| Assertion | Contract content | Gate |
| --- | --- | --- |
| Dataset description | Table or document meaning | Current asset evidence and owner review |
| Field description | Column meaning | Current field evidence; optional policy/glossary citations |
| Grain | Row meaning and key fields | Current table fields; owner review of business grain |
| Join rule | Direction, paired columns, cardinality, caveat | Both current assets cited; owner review |
| Metric | Name, meaning, measure field, aggregation, unit | Current field evidence and owner review |
| Query example | Question, dialect, read-only SQL, expected grain | SQL source only; stored without execution verification |

Document headings and bounded excerpts become glossary candidates. A document can be connected by itself or alongside tables. `search_document_evidence` returns a current `connection_id`, `snapshot_id`, and `evidence_id`; a structured-source assertion can cite these via `evidence_refs`. If that document connection changes, its accepted assertion no longer applies to the current contract. The old proposal stays visible as stale. The default connector setting does not retain source values or excerpts; the operator or user must explicitly allow bounded retention when they want document text to ground semantics.

Protocol 1.3 inventory observations can also support an existing target assertion
through [exact manifest, entity and proof references](inventory-semantic-citations.md).
The target reviewer approves the local interpretation; native metadata remains
untrusted evidence. Currentness checks suspend dependent meaning after inventory
changes and apply to metric plans, guidance and task-context receipts.

A document-only connection also builds candidate glossary nodes and evidence edges. Matching headings across documents can produce possible-duplicate or conflicting-definition edges for review. An agent can draft a glossary definition from the document evidence; after owner approval, the definition appears in the semantic contract, graph API, and RDF projection. Reviewed table and field descriptions also appear in the graph API. If a cited document changes, the dependent field node loses its approved description until it is reviewed against current evidence. The platform does not assume that terms with the same spelling have the same business scope across connections.

`get_federated_evidence_graph` and its HTTP equivalent show explicit citation paths across separately connected sources. The UI's **Document links** view shows the same bounded graph for a selected source, including pending, approved, and stale assertions. It does not infer cross-source entity equivalence. Graph nodes carry connection and snapshot IDs so an agent can distinguish the structured source from a supporting policy or glossary document.

Scoped source contracts and term lookups also expose `cited_glossary`: approved document definitions supported by an approved, effective source assertion's exact current citation. Each entry retains both document provenance and the originating source assertion/asset/field. Source filtering remains intact; uncited, pending, stale or inaccessible definitions are excluded. The definition's review state stays separate from any inferred term identity or mapping.

Protocol 1.2 imports can also support a different connection's field, dataset or metric assertion through a full `evidence_refs` handle. A pending draft may cite raw observed evidence, but approval requires a current local meaning review anchored to the exact imported definition. `cited_upstream_definitions` preserves the native definition, namespace, content revision, optional unit/formula and unknown native clocks/version alongside the local interpretation and originating source assertion. Both reviews and grants must remain current; an upstream source error, changed revision or archived meaning suppresses the citation and suspends the dependent assertion. Native formulas remain metadata and do not establish a join or executable metric.

## Consuming terms and metrics

Use compact `resolve_context`, advanced `resolve_business_term`, or HTTP `/agent/api/context` with an explicit `connection_id` when the environment or business unit is known. The response separates:

| Result | Meaning | Next step |
| --- | --- | --- |
| `matches` | Local concept label matches, with the concept's own trust state | Inspect definition, evidence and source bindings |
| `metric_matches` | Current approved metric label matches | Follow `contract_ref` to the asset contract |
| `cited_glossary` | Source-linked reviewed document label matches | Retain document and originating source assertion provenance |
| `cited_upstream_definitions` | Source-linked reviewed imported definition label matches | Retain native metadata, local interpretation and originating assertion separately |
| `retrieval_candidates` | Relevant definition phrases or reordered/plural tokens | Confirm meaning and scope; `requires_confirmation=true` |

For example, “Sim Lab execution costs” can suggest both recorded completion cost and estimated cost. The harness must choose the definition answering its question. “Player projection input snapshot” can suggest a cited projection-serving definition without creating a new local entity or join. Matching “gross revenue” in “Net revenue excludes gross revenue” yields a relevance suggestion, never an approved gross-revenue metric. Matching text does not establish an approved alias or equivalent identity.

Suggestions reuse lexical ranking and source metadata without requiring an embedding download or external model. Meaningful query tokens must occur in metadata for token suggestions; arbitrary synonyms remain unsupported. Reviewed metric and glossary candidates take priority over inferred concepts. Repeated citations to one glossary term on the same asset consume one suggestion slot; the full contract retains all citations. Graph expansion and outputs remain bounded, with total-match and more-available counts. A scoped metric-only lookup avoids reading other sources' concepts.

Each eligible approved metric carries `query_guidance`. It binds the connection, registry snapshot/version, source asset/pointer, field/native type and exact effective approval/evidence. Stale or missing fields, ambiguous field names, absent evidence and unproven approval flags do not supply an executable metric binding. The guidance exposes:

- `value_policy`: NULL is missing; observed numeric zero remains a value; refuse an absent field rather than substitute another source's measure.
- `aggregation_policy` and `diagnostics`: count selected, known, missing and applicable zero rows. Non-count aggregates with no known measures return NULL; `zero_count_proves_zero_measure_or_spend=false` applies to every metric's count diagnostics. The harness must verify provider-specific behavior, including aggregate functions that otherwise return a default.
- `comparison_policy`: paired-measure comparisons use the same non-null row cohort. Other comparisons preserve their declared cohorts.
- `source_state`: current refresh status/error, prior refresh clock and readiness. A retained approved snapshot can coexist with `source_unavailable`; `current_schema_verified=false` prevents interpreting registry metadata as a live schema check.
- `execution`: the customer harness executes SQL; schema, native aggregation, filters, grain and cardinality still require verification. Counts/results are `not_computed`, and query examples remain unexecuted.

The guidance is derived planning advice, not another owner-approved business assertion or SQL runtime. It does not invent units, currency conversion, missingness values, cross-source identity or successful execution. Context and contract transports retain complete selected bindings within the shared JSON budget; an oversized response refuses explicitly.

`get_ontology_export(connection_id, version=None)` on both MCP surfaces returns a complete authorized Turtle export with its snapshot ID/version. The tool shares the exact UTF-8 JSON payload budget used by machine contracts and refuses oversized exports. RDF includes definition review status, proposal, evidence and decision provenance alongside the concept's original status. HTTP consumers can discover the mounted API at `/agent/api/openapi.json` and request `/agent/api/sources/{connection_id}/ontology.ttl?version=...`.

Source status now includes `stale_assertion_count`. A changed citation makes the affected approved meaning inactive, clears current agent readiness, and raises a review signal even when the structured source itself has not refreshed. A prior approval cannot be reused against the changed document; the agent must inspect the new evidence and submit a replacement proposal.

The local source profiler reads at most the configured sample of CSV key fields in memory and stores only key-overlap counts, target uniqueness, sample completeness, and grain candidate statistics. A full sample can support an observed join coverage claim; it still cannot establish business identity, effective time, or permission to use a join. The document scanner accepts Markdown, text, HTML, DOCX, and text-bearing PDF within configured size and text budgets. A PDF with no extractable text reports `no_text_or_ocr_required` instead of inventing glossary entries. The external adapter protocol can carry up to 50 bounded document term excerpts after explicit opt-in.

`get_semantic_contract` is the native contract: source bindings, observed schema, glossary, reviewed assertions, pending drafts, stale assertions, joins, metrics, and SQL examples with trust state. It is available over MCP and HTTP. `get_ossie_preview` validates a SQL-source projection against a pinned [Apache Ossie core schema](https://github.com/apache/ossie/blob/b6c702ed1c07e91382a69e870c875cbd19570828/core-spec/ossie-schema.json). The pinned spec is `0.2.0.dev0`, an in-development format; the response says `draft_valid` when its shape validates, not that the source meanings or SQL work. The preview maps approved table/field descriptions, grain keys, suitable joins, and basic metrics. Document context, proposal workflow, evidence, and broader ontology remain in the native contract and RDF graph. Apache Ossie was formerly called Open Semantic Interchange (OSI); the [project repository](https://github.com/apache/ossie) describes its role as a vendor-neutral interchange format.

The demo gateway uses a single organization behind its configured agent bearer token. Managed local-owner, OIDC and IAP profiles enforce their own browser and agent boundaries. Warehouse drivers and credentials still need operator provisioning and provider validation. Knowledge Layer supplies reviewable context; the customer harness executes separately authorized queries and verifies results, joins, performance and conclusions. Synthetic examples do not qualify customer execution or enterprise identity.