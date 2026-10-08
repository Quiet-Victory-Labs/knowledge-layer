# Reviewed single-asset composite metrics

`metric-composite-v1` extends the existing owner-reviewed metric contract and
`plan_metric_query` tool. It supports at most eight aggregate measures over one
native SQL asset, with a bounded numeric expression over their declared aliases.
The planner returns parameterized SQL and verification obligations; the registry
does not execute source queries or return a computed metric result.

This profit-margin definition computes `(SUM(amount) - SUM(expense)) / SUM(amount)`
over one shared cohort. It does not average individual row margins.

```json
{
  "schema_version": "metric-composite-v1",
  "name": "Profit margin",
  "description": "Ratio of summed profit to summed revenue in one complete-case sales cohort.",
  "measure_field": "amount",
  "aggregation": "composite",
  "unit": "ratio",
  "dimensions": ["region"],
  "filters": [],
  "time": null,
  "cohort": {
    "description": "One sale per exact source id.",
    "key_fields": ["id"]
  },
  "additivity": {"kind": "non_additive", "non_additive_dimensions": []},
  "null_policy": "complete_case",
  "zero_division": "null",
  "measures": [
    {"alias": "revenue", "measure_field": "amount", "aggregation": "sum", "unit": "USD"},
    {"alias": "cost", "measure_field": "expense", "aggregation": "sum", "unit": "USD"}
  ],
  "expression": {
    "op": "divide",
    "left": {
      "op": "subtract",
      "left": {"op": "measure", "alias": "revenue"},
      "right": {"op": "measure", "alias": "cost"}
    },
    "right": {"op": "measure", "alias": "revenue"}
  }
}
```

The anchor `measure_field` must equal the first declared measure field. Measure
aliases are unique bounded names. Each aggregate uses a known scalar field and
one of `sum`, `avg`, `min`, `max`, `count`, or `count_distinct`; numeric aggregates
require native numeric metadata. Counts declare `records`. Other declared units
must agree with physical unit metadata when supplied.

The expression accepts `measure`, finite numeric `literal`, `add`, `subtract`,
`multiply`, and `divide` nodes, with at most 31 nodes and depth five. All declared
measures must participate. Addition and subtraction require matching units.
Multiplication permits dimensionless scaling (`1` or `ratio`); equal-unit
division produces `ratio`, and other division preserves the declared quotient
unit, such as `USD/records`. The final declared unit must match the expression.
There is no currency conversion. Nonlinear aggregate expressions and aggregates
using averages, distinct counts or extrema require `non_additive` behavior.

All measures share the reviewed asset, key fields, filters, dimensions and time
binding. Per-measure overrides refuse. Optional request filters only narrow this
common scope; they cannot remove reviewed filters. The existing UTC/date time
binding and half-open requested time ranges apply to every measure. Native rows
with any missing participating measure field are excluded from every aggregate.
The result includes `selected_rows`, `known_measure_rows`, `missing_measure_rows`,
`missing_key_rows`, and each aggregate value with its alias mapping. An empty
complete-case cohort produces NULL, including count expressions. A zero
denominator produces NULL; an observed zero numerator with a nonzero denominator
remains zero. These policies are explicit and cannot be overridden by requests.

Production planning loads the current authorized published source, effective
approved assertion, native schema, formula digest and grain from the registry.
Composite key fields must exactly equal the current approved grain keys. Review
or publication claims supplied by the caller are not authorization. Publication
withdrawal, grain archival, formula drift, source errors and unavailable access
refuse planning. Existing `metric-v1` and `metric-v2` compatibility is retained.

The plan includes a separate bounded duplicate-key diagnostic and requires the
external authorized consumer to verify live schema, source permission, key
uniqueness and nonnull keys, numerical precision/overflow, temporal precision,
scan budget and output truncation before interpreting any result. Registry grain
approval is a reviewed definition, not proof that live rows satisfy it. SQL is
bounded to 32 KiB and 400 parameters, with a maximum requested output of 1,000
rows plus one row for truncation detection; output limits do not bound scanned
rows.

SQLite effects are exercised against real local tables. PostgreSQL and ClickHouse
have compiler tests, not provider execution qualification. Division uses
PostgreSQL NUMERIC or SQLite/ClickHouse binary64 arithmetic. SQLite and ClickHouse
decimal/numeric composite fields refuse rather than claim exact arithmetic.
Unsupported dialects, multiple assets or sources, joins, custom functions and
arbitrary SQL refuse. Composite definitions remain intact in the native semantic
contract; the draft Ossie preview explicitly omits them and reports a lossy
projection until an interchange mapping exists.
