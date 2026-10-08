# Native CLI consumption qualification

Knowledge Layer exposes reviewed meaning and plans; the customer's harness uses
its independently authorized source tools. Native-client acceptance must observe
real MCP calls and source results. A connected endpoint, exported skill or an
agent's success message alone does not qualify that journey.

## Run the opt-in reference drill

Install the existing base dependencies and authenticate your chosen CLI normally.
The drill uses that native authentication without reading or copying credentials.
It makes model requests against disposable synthetic data. It does not connect
customer systems and does not install or change agent configuration.

```sh
uv sync --frozen
uv run --no-sync python scripts/drill_native_harness.py --client codex \
  --codex-model gpt-6-astra --output .local/native-harness/codex.json
uv run --no-sync python scripts/drill_native_harness.py --client claude \
  --output .local/native-harness/claude.json
```

Choose a model your own account supports; the Codex reference name above is not a
required product dependency. Omit a model flag to use the native CLI default, or
provide `--claude-model`. Requested models and models reported by the CLI are
separate receipt fields. `--client both` runs sequentially. CLI wall time is
240 seconds per phase by default, configurable from 30 to 600 seconds; captured
stdout/stderr each stop at 2 MiB. Claude receives a USD 3 budget per phase.
Codex has no monetary cap in this drill; its actual reported token usage is
recorded. CLI/account limits remain independent of those bounds. Model-dependent
runs are opt-in and are not part of ordinary CI.

The existing enterprise example supplies finance and sales SQL sources plus a
policy document. The operator explicitly reviews and publishes fixture meaning.
Each native client then discovers tools, resolves company guidance, retrieves
task context, revalidates its receipt, and requests both reviewed metric plans.
A separately mounted fixture source MCP executes only server-observed approved
plans through SQLite `mode=ro` and `query_only`. It independently rechecks current
meaning and receipts, limits results and time, and verifies row-grain diagnostics.
It accepts no arbitrary SQL. This reference guard belongs to the test harness;
Knowledge Layer gains no customer query-execution endpoint.

Gold answers stay outside the prompt: booked finance revenue is 20 USD, including
one measured zero and one missing amount; sales pipeline is 1,040 USD. Overlapping
cohorts leave a company total unknown. After the steward withdraws finance
publication and company guidance, a new native session receives the cached
receipt. Acceptance requires denials on that exact receipt and metric handle,
current guidance resolution, a refused final answer, and no additional source
executions. Different or malformed inputs cannot satisfy the withdrawal gate.

## Isolation and evidence

The drill creates two nonce-scoped loopback MCP mounts using the existing compact
server, bearer boundary and bounded envelope. Authentication values remain in
the child environment; temporary MCP configuration contains only an environment
reference. CLI tools are explicitly scoped; built-in shell, files and web tools
are disabled or refused by the verifier. Claude's known native builtin plugins
are permitted; additional plugins, hook events and additional MCP servers refuse
qualification. Managed client policies still apply. Skill autoactivation is not
tested here.

Reports use owner-only regular files, rejecting symlinks, hardlinks, public files,
foreign ownership and nonregular destinations before models run. Raw prompts,
CLI output, tokens, signed receipts and driver messages never enter reports.
Failure codes and quota categories are finite; a quota refusal cannot become a
pass. Runtime shutdown verifies owned process-group removal, including surviving
descendants, and stops the owned loopback server. Source-file hashes and selected
user configuration readbacks must remain unchanged. Native CLI authentication may
maintain its own credential/cache state; those files are not copied or restored.

## Correct MCP effect hints

Registered metadata, context, planning, evidence, ontology and guidance readers
now advertise literal read-only, non-destructive, closed-world and idempotent
hints. Mutations retain conservative classification. Advanced semantic search
remains conservative because cold FastEmbed initialization can download weights
and write a local cache. These annotations describe effects; authorization,
source grants, currentness and publication checks remain enforced by the service.

Before this fix, Codex 0.158 initialized and listed tools but denied read calls
before HTTP under `auto` plus `approval_policy=never`. Its
[approval classifier](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/mcp_tool_call.rs#L2442)
treats missing annotations conservatively. Accurate hints allow this reference
read journey without bypassing the CLI sandbox or approving mutation tools.
CLI behavior is version-specific: see [Codex configuration](https://developers.openai.com/codex/config-reference)
and [Claude headless usage](https://code.claude.com/docs/en/headless).

## Qualification boundary

The clean `ad8cd10` checkout passed **1,339 tests** and the native Codex drill on
October 3, 2026. Codex CLI **0.158.0**, with requested model **gpt-6-astra**,
negotiated MCP **2025-06-18**. It returned both exact known revenues, the separate
missing/zero counts and an unknown combined total; withdrawal refused the exact
cached receipt and finance metric, with no further source executions. Both SQL
fixture hashes and selected user configuration files remained unchanged; the
owned runtime and fixtures were removed. The CLI JSON events do not independently
report the provider's resolved model identity; the receipt preserves the requested
model separately rather than inventing an observed model.

The Claude Code **2.1.284** replay cannot qualify consumption: its authenticated
account rejected requests at its weekly usage limit. No reset, purchase or
alternative credentials were attempted. Its failed receipt remains separate
from the passing Codex receipt. Native Claude end-to-end qualification remains
open; normal CI exercises the model-free guards, not a paid native model run.

Private receipts: ignored `.local/native-harness/codex-clean-ad8cd10.json` and
`.local/native-harness/claude-clean-ad8cd10.json`. The clean regression retains
the existing Alembic SQLite expression-index reflection warning. Ruff and the
secret gate passed; scanner rules and the baseline were unchanged. Fresh unsigned
archives additionally exercise the reusable example outside the checkout through
the existing distribution verifier. No artifact has been published or signed.

The reference covers synthetic reviewed semantic consumption over native HTTP
MCP and independent fixture reads. It does not establish model uplift, native
skill installation/activation, OAuth discovery/login from these clients, customer
IdP mappings, source row/field/document ACL mirroring, proposal/publication denial,
expired-copy or registry-outage behavior in native clients, production HTTPS,
other operating systems or a universal client/model compatibility guarantee.
Those remain separate gates in the [completion plan](platform-completion-plan-2026-10-03.md).
