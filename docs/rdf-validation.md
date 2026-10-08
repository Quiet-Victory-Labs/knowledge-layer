# Offline RDF projection validation

Knowledge Layer can check a saved RDF/Turtle snapshot export against its bundled
SHACL Core shapes. This is optional offline verification of the exported graph's
structure. PostgreSQL remains the knowledge registry, and the service's Python
validators and review/publication rules remain authoritative.

## Install and run

Install the optional `semantic-validation` extra, which selects
`pyshacl>=0.40.1,<0.41`:

```sh
uv sync --frozen --extra semantic-validation
```

The contributor `uv sync --frozen --all-extras` command also includes it. The
default base package and Docker image do not require this extra.

Save an existing authorized ontology export as a UTF-8 Turtle file, then run:

```sh
uv run --no-sync python -m knowledge_layer.rdf_validation --input /path/to/ontology.ttl
```

An installed package can use `python -m knowledge_layer.rdf_validation` with the
same arguments after installing its `semantic-validation` extra. The validator
loads [the canonical shapes](../src/knowledge_layer/schemas/knowledge_shapes.ttl)
through package resources, so source checkouts and installed wheels use the same
file. It does not require a `semantic/` directory beside the installation.

The CLI accepts an explicit local regular file and refuses symbolic links,
non-regular files, changed input and unavailable input. Turtle input is limited to
5 MiB of UTF-8 bytes, and the parsed graph to 50,000 triples. These are input and
graph budgets, not a process-memory or elapsed-time guarantee.

## Results

The CLI writes one JSON receipt to standard output:

| Result | Exit code | Meaning |
| --- | --- | --- |
| `passed` | 0 | The bounded graph conforms to the bundled projection shapes. |
| `failed` | 1 | Validation completed and found structural violations. |
| `refused` | 2 | Input, dependency or validator conditions prevented a validation result. |

Completed validation receipts use `schema_version: knowledge-rdf-shacl-v1` and
include `shapes_sha256`, `conforms`, `triple_count`, `violation_count`,
`violations_truncated` and `inference: none`. At most 100 violation records identify
the focus-node IRI, property-path IRI when present, and constraint component.
Offending values, source text and parser/validator diagnostics are excluded.
Refusals have `conforms: null` and a bounded code, such as
`semantic_validation_extra_required`, `invalid_turtle`, `input_too_large` or
`graph_too_large`; they do not report successful conformance or validation counts.

## What the shapes check

The project-owned shapes describe one snapshot projection: local snapshot and
connection identity, generated-node provenance, concept labels and review
references, relationship endpoints, field metadata, proposal payload typing, and
snapshot or inventory citation bindings. Metric certification and query execution
flags must stay false. Inventory citations bind a manifest rather than an invented
semantic snapshot, and snapshot projections cannot assert `owl:sameAs` identity.

The profile preserves legitimate draft and historical states. Candidate definitions
can be absent; approved definition review remains separate from candidate identity
mapping. Unknown source metadata can be omitted, and external or historical
reference targets need not be copied into the local graph. Regression coverage
includes discovery, reviewed definitions and contracts, historical proposals,
external snapshot/inventory citations, and deliberately invalid graph structures.

## Verification boundary

Only the bundled SHACL Core shapes execute. Inference is `none`; JavaScript,
advanced features, rule iteration and OWL imports are disabled. Callers cannot
supply custom shapes. Turtle IRIs remain references and are not fetched.

This command is not part of HTTP/MCP export requests or the publication gate. It
does not import RDF into the registry, change review/readiness state, execute source
queries, or perform general OWL reasoning. Conformance establishes this structural
profile only; it does not establish source accuracy, authority, permissions,
freshness, completeness or business truth. Existing source grants, evidence
currentness checks, owner review and steward publication remain separate controls.
