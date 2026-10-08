# Public synthetic enterprise development corpus

80 questions, two business units, 20 questions per category. All data is synthetic
and deliberately public, with exact gold contracts and complete source tables. This
is a development/regression corpus, already examined during implementation; it is
not a frozen holdout, independent steward label set, vendor feed or customer sample.

- `source_data.json`: eight typed tables and 22 scoped policy/definition passages.
- `contracts.json`: 22 exact synthetic meanings, including two different Revenue
  meanings, native identity namespaces, a deliberately non-name-based mapping,
  missing native clocks/versions, and pending/untrusted meaning.
- `benchmark.json`: 80 questions, source digest bindings, contextual grants, explicit
  failure conditions, exact gold answers/refusals and fixture derivations.
- `build_corpus.py`: deterministic source-free regeneration. It validates the entire
  corpus before writing. A label/data change changes content digests and invalidates
  existing result files. The builder is fixture authoring, not a model/source runner.

There are 56 answered questions and 24 explicit refusals. Twenty numeric metric
questions are sanity controls (`value_target_case: false`); the 60 remaining value
cases include scoped definitions, cross-unit differences, identity ambiguity,
provenance/refusals and permission boundaries. Numeric checks cover posted versus void
or draft, accrual versus cash, one-to-many payment grain, mixed currency, all-null
versus measured zero versus empty cohort, SLA measured denominators and negative refunds.
Context refusals for failed sources, revoked reviews and incomplete inventory are
declared synthetic conditions; the offline grader does not simulate a real provider.
Fixture schemas are invented specifically for this corpus and claim no native feed compatibility.

Gold content is separately derived and validated. Full document text must be returned
exactly, with exact source/contract citations. No truncated source projection or fuzzy
identity answer passes. A refusal cannot leak an unauthorized contract or citation.
Unknown telemetry, native clocks and versions remain null rather than fabricated.

Use `result_template` from `knowledge_layer.evaluation` to create anonymous pinned arm
files. Fill results with the actual run responses and externally recorded observations;
do not report test gold copies as model runs. The fixture test helper explicitly names
its harness/model `fixture-harness` / `fixture-no-model` and synthetic trace handles.
The CLI grades result files only. See `docs/capability-support-matrix.md` for interfaces,
uncertainty assumptions and qualification limits.
