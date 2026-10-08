<p align="center">
  <img src="docs/assets/knowledge_layer_logo.jpg" alt="Knowledge Layer" width="96" height="96" />
</p>

# Knowledge Layer

Self-hosted, evidence-backed semantic context for external agents.

[![Verify CI](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/workflows/verify.yaml/badge.svg)](https://github.com/Quiet-Victory-Labs/knowledge-layer/actions/workflows/verify.yaml)
[![Apache 2.0](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)

Knowledge Layer connects scoped data and document sources, preserves metadata
and cited evidence, and lets agents propose definitions, relationships and
metrics for human review. Reviewed, current context can be published through
Model Context Protocol (MCP), HTTP APIs and graph exports.

The project is **alpha**. Connector availability, offline conformance tests,
local installation checks and customer production qualification are distinct.
See the [capability matrix](docs/capability-support-matrix.md) for supported
paths and their limits.

## What it provides

- Versioned connections, discovery status, source evidence and drift tracking.
- Typed table, field, grain, join and metric proposals with exact evidence bindings.
- Human review and separate publication of current context for agent use.
- Scoped glossary and ontology records, a collective knowledge map, and
  Resource Description Framework (RDF) exports. Optional Shapes Constraint
  Language (SHACL) validation checks exported graph structure.
- Bounded work handoffs for external agents, with drafts and uncertainties kept
  separate from accepted knowledge.
- One-way catalog export translation for selected Alation, Collibra, Atlan,
  OpenMetadata and DataHub payloads. Translation alone does not import or
  publish records.

The service supplies context and query guidance. Actual source queries and
verification of analytical results belong to separately authorized source tools.
Publication records owner review; it does not certify business truth or promise
hallucination-free answers.

## Start a local installation

The portable installer requires Docker Engine with Compose and Python 3.11 or
newer on Linux or macOS. It builds the application from this checkout. Native
Python development requires Python 3.14; frontend development uses Node 26.10
and npm 12.2.

```sh
git clone https://github.com/Quiet-Victory-Labs/knowledge-layer.git
cd knowledge-layer
./knowledge-layer start
```

Open [http://127.0.0.1:4173/](http://127.0.0.1:4173/), use the private bootstrap
token from the launcher to create the local owner account, and follow Setup.
Add source folders to the installation's read-only `sources/` mount before
connecting them. The default profile enables local files; remote connectors
require operator configuration.

To configure an agent for a project:

```sh
./knowledge-layer agent --client codex --project /absolute/path/to/project
# Also supports --client claude and --client cursor.
```

Use the configuration and credential reference shown by Setup, then ask the
agent to call `get_build_handoff` and report its visible sources. A saved config
file alone does not prove a working connection. Agents draft knowledge; the
human reviews and publishes it.

```sh
./knowledge-layer stop
```

See [self-hosting](docs/self-host.md) for source configuration, credentials,
backup and recovery. Organization identity and cloud deployment require the
additional controls in the [OIDC pilot](docs/oidc-pilot.md) and
[cloud deployment guide](docs/cloud-deployment.md).

## Try the Python SDK

Install from the checkout; **PyPI publication is not configured**. There is no
`knowledge-mcp` console executable. Use the running HTTP MCP endpoint or embed
the Python server explicitly.

```sh
uv sync --frozen --all-extras
uv run --no-sync python examples/01_quickstart_sdk.py
uv run --no-sync python examples/02_catalog_federation.py
```

The [SDK example](examples/01_quickstart_sdk.py) creates disposable synthetic
files, discovers metadata, reviews a specific grain assertion, publishes the
current reviewed snapshot, and checks that the SDK's MCP server can retrieve
that same source. It neither runs a model nor queries a customer system.

The [catalog example](examples/02_catalog_federation.py) translates synthetic
exports with explicit scope and completeness. It checks that translation does
not persist records. Incomplete pagination is refused, and unknown observation
metadata stays unknown. See [catalog integrations](docs/catalog-integrations.md)
for the distinct translation and adapter-ingestion steps.

## Source access and retention

Knowledge Layer does not write connected enterprise sources. Native database
and Iceberg discovery use metadata paths; local structured-file profiling reads
rows to compute bounded profiles. Document evidence retains cited passages.
Optional semantic sampling can retain bounded entity IDs, labels and edge
values. This is **not an unconditional zero-row or zero-content guarantee**.

Provision restricted source credentials, choose excerpt/sample retention and
agent egress policy, and split connections along audience boundaries. Registry
grants do not mirror upstream row, field or document permissions. See
[architecture and trust boundaries](docs/architecture.md),
[source grants](docs/source-access.md), and
[connector qualification](docs/native-connector-qualification.md).

## Evidence and qualification

The repository includes offline tests, synthetic consumer/evaluation fixtures,
local installation and recovery drills, and scoped connector qualification
records. These establish the declared behaviors and workloads, not a general
production-scale latency benchmark, billion-row processing performance, model
uplift, or customer authorization.

Start with the [capability matrix](docs/capability-support-matrix.md),
[local qualification runbook](docs/local-qualification-runbook.md), and
[release readiness requirements](docs/release-readiness.md). Graph validation
checks structure; human review and source evidence remain necessary.

## Development

```sh
uv sync --frozen --all-extras
uv run --no-sync ruff check src scripts tests examples
uv run --no-sync pytest -q
npm ci --prefix web
npm --prefix web test
npm --prefix web run build
```

[Contributing](CONTRIBUTING.md) includes the backend/Vite development flow,
browser smoke tests and integration checks. Verify CI runs the Python suite,
SDK examples, frontend tests/build, browser smoke and deployment drills.

## Documentation and governance

- [Documentation index](docs/README.md)
- [Agent build, review and consumption](docs/agent-build-flow.md)
- [Catalog and lakehouse integrations](docs/catalog-integrations.md)
- [Security reporting](SECURITY.md) and [audit event limits](docs/audit-events.md)
- [Release process](RELEASING.md), [support](SUPPORT.md) and [governance](GOVERNANCE.md)

Released under [Apache 2.0](LICENSE). Third-party notices are in [NOTICE](NOTICE).
