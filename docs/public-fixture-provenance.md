# Public example and fixture provenance

This inventory describes the intended public alpha source tree. It records
repository evidence and the sanitation performed on October 8, 2026. It does
not certify authorship, customer consent, redistribution rights or a human legal
review. Those decisions must be recorded separately by the responsible owner.

| Material | Repository evidence and scope | Limits |
| --- | --- | --- |
| `data/connectable/northstar/` | Declared synthetic service-company fixture: fictional customer labels, asset IDs, work orders and conflicting policy notes. Used for local discovery and reviewed-context demonstrations. | Repository declarations are the provenance evidence; no independent authorship or rights approval is recorded here. Treat the policies as test inputs, not real company instructions. |
| `data/connectable/juniper/` | Declared synthetic commerce fixture: product CSV, example orders and a supplier-item specification. | Example records do not establish a real supplier relationship or business metric. Independent rights approval is not recorded here. |
| `tests/fixtures/enterprise_benchmark/` | The fixture README describes a generated, examined finance/support corpus. `build_corpus.py` is the generation recipe; source rows, contracts and answer cases are retained for reproducibility. | An examined corpus is a regression fixture, not a fresh holdout or evidence of model uplift. Generation and a project license do not themselves establish an independent rights review. |
| [SDK quickstart](../examples/01_quickstart_sdk.py) | Creates disposable illustrative data inside a temporary directory and uses a local registry. | No customer system or model quality evaluation. Published fixture context remains separate from certified business meaning. |
| [Catalog translation](../examples/02_catalog_federation.py) | Uses hand-authored synthetic native records, explicit source scope and explicit synthetic observation values. | Translation does not persist an import, establish native permissions or prove a vendor account integration. |
| [Synthetic reference adapter](../examples/reference_adapter.py) | Bounded adapter example containing hard-coded illustrative CRM metadata. Its module explicitly identifies the records as synthetic and supplies no live vendor connection. | No project-specific private account, table inventory or live-provider qualification is supplied by the example. |
| [Synthetic company guidance](../examples/company-guidance-synthetic.json) | An illustrative procedural template scoped to `synthetic-analytics`. It contains no source records or invented connection dependencies. | A draft procedure is not adopted company policy, source truth or authorization to execute another tool. |

The retained `docs/assets/knowledge_layer_logo.jpg` is abstract project artwork,
not a customer screenshot. Visual inspection found no source records, credentials
or operational details. Its creator/rights still require owner confirmation;
visual inspection is not authorship or redistribution approval. The architecture
artwork claiming unconditional zero-row retention and cryptographic evidence,
and the unused banner implying vector/RAG components, have been excluded.
The architecture guide uses its scoped source/agent/steward Mermaid flow instead.

## Material excluded from the public tree

Private source-specific pilot reports were removed from the current public docs.
Those reports contained private repository revisions and paths, operational
findings, actual source/provider values and project-specific topology. Their
original bytes were preserved and hash-checked in an ignored, owner-only local
archive before removal. Public technical guides retain generic boundaries and
validation plans, without rebranding the removed measurements as synthetic
test results.

This is a current-tree sanitation record. It does not claim that older Git
objects, remote refs, release artifacts or private receipt directories are safe
to publish. The maintainer must separately choose and verify the public history
and retain any source/fixture rights decision outside this automated inventory.

## Future contributions

Use original synthetic records or material with an explicit, verified public
license. Record its creator, generation recipe or immutable source, license and
any required attribution. Do not copy customer records, private repository
content, credentials or operational findings into examples, fixtures, issues or
screenshots. Review the [security policy](../SECURITY.md) and
[dependency and data-rights review](dependency-license-review.md) before release.
