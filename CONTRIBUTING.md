# Contributing

The product boundary is a small self-hosted knowledge service: connect scoped sources, preserve versioned metadata and evidence, let external agents draft semantic meaning, and require owner review before accepting business claims. Keep the main UI focused on connection, status, and decisions.

## Local verification

```sh
uv sync --frozen --all-extras
uv run --no-sync ruff check src scripts tests examples
uv run --no-sync pytest -q
npm ci --prefix web
npm --prefix web test
npm --prefix web run build
uv run --no-sync python examples/01_quickstart_sdk.py
uv run --no-sync python examples/02_catalog_federation.py
helm lint charts/knowledge-layer -f charts/knowledge-layer/ci-values.yaml
```

For frontend development, run the backend and Vite in separate terminals:

```sh
uv run --no-sync uvicorn knowledge_layer.platform:app --host 127.0.0.1 --port 8000
npm --prefix web run dev
```

Vite forwards `/api`, `/auth`, and `/agent` to the same backend. Set
`VITE_API_TARGET` when using a different backend port. This is the local demo;
use the self-host launcher for owner authentication. Browser smoke tests start
their own isolated demo registry and servers:

```sh
npm --prefix web exec -- playwright install chromium
npm --prefix web run build
npm --prefix web run test:e2e
```

For the small live path, run `./scripts/start.sh`, then `uv run --no-sync python scripts/evaluate_quick_agent.py`. The [agent skill](skills/knowledge-layer/SKILL.md) describes how an end-user agent should handle evidence and abstain from unsupported meaning.

The `local-integration` CI job starts the synthetic Compose profile and exercises PostgreSQL migration/adoption, durable refresh jobs, backup/restore, restricted connector grants, and the authenticated MCP gateway. Run the same checks with the [local qualification runbook](docs/local-qualification-runbook.md) before changing schema or connector boundaries. The OIDC application and worker must never create tables during startup; add an immutable revision and test both a fresh upgrade and an upgrade from the previous release.

The `self-host-integration` job builds the shipped application image once and
tests a fresh portable owner installation through real HTTP and native MCP.
It covers worker discovery of synthetic structured and document files, durable
draft checkpoints, exact human acceptance, published retrieval, restart, cold
backup and recovery into a separate installation, and withdrawal. Run it locally
against an image built from the same checkout:

```sh
docker build --tag knowledge-layer:verify .
docker build --file Dockerfile.postgres --tag knowledge-layer-postgres:verify .
python3 scripts/drill_self_host.py --image knowledge-layer:verify --postgres-image knowledge-layer-postgres:verify --output .local/self-host-result.json
```

The drill uses private temporary files and nonce Compose projects, removes its
containers, credentials, data and image aliases, and preserves the caller's
image. A cleanup failure retains private retry files and fails qualification.
It invokes no model or cloud service; client configuration checks do not qualify
Codex, Claude or Cursor inference behavior or customer source permissions.

Connectors must declare their scan scope, retention behavior, permissions, limits, change signal, and safe failure mode. Keep source credentials behind named operator references. Do not add a menu choice as a claim of vendor support; use a real credentialed compatibility test before calling a dialect qualified. New ontology assertions need source identity, snapshot version, evidence handles, trust state, and a review path. Tests should cover denied access, stale evidence, source drift, and false merges rather than simply mirroring implementation code.

External adapter contributors can run the [offline contract check](docs/adapter-conformance.md)
against an explicit protected local response file or synthetic fixture. The online
connector and offline helper share one validator for versions 1.0–1.2; the receipt
contains safe counts/digests, not source content. This check is necessary wire
evidence, while native source permissions, completeness and read-only credentials
require independent qualification. No vendor SDK or hosted model is required.

Decision-provider adapters implement the small `DecisionProvider.assess(packet)` contract in `src/knowledge_layer/decisions.py`. Keep them optional and outside the base image when they need model runtimes. Bound outbound metadata, require explicit permission for remote egress, validate typed options and probability distributions, and preserve the steward approval boundary. A new provider or routing policy needs a held-out labeled evaluation that reports false skipped work and calibration against the rules baseline. A high model score is not source evidence.

Open a focused issue or pull request with the customer scenario, observed behavior, test evidence, and any compatibility or security limits. Do not include private customer data or credentials. See [SECURITY.md](SECURITY.md) for private vulnerability reporting.

QVL maintainers review pull requests under [GOVERNANCE.md](GOVERNANCE.md). Changes to security boundaries, public contracts, migrations, or publication behavior need an explicit compatibility and threat review in the pull request. Keep the changelog current for user-visible changes. Releases follow [RELEASING.md](RELEASING.md); contributors must not move release tags or publish artifacts from unreviewed branches.
