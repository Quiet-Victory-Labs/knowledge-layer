# Your first reviewed knowledge

Start with the bundled synthetic data, connect one of your existing agents, and
review a small set of claims. The result is current, evidence-backed context that
the same agent can retrieve for a later task. The model runs in your chosen agent
client; Knowledge Layer stores source evidence, drafts and review decisions.

## Install locally

You need Docker Engine with Compose and Python 3.11 or newer on Linux or macOS
with Linux containers. The default installer builds from this checkout and does
not require a model account, cloud service or host Node installation.

For a fresh installation:

```sh
git clone https://github.com/Quiet-Victory-Labs/knowledge-layer.git
cd knowledge-layer
./knowledge-layer init --examples
./knowledge-layer start
```

`init --examples` copies the synthetic Northstar and Juniper folders into the
installation's read-only source mount. Initialization refuses an existing
installation; if you already have one, use `./knowledge-layer start` and follow
the [self-hosting guide](self-host.md) to add example folders.

Open [http://127.0.0.1:4173/](http://127.0.0.1:4173/). Read the bootstrap token
from the private file named by the launcher, or display it explicitly:

```sh
./knowledge-layer bootstrap
```

Use that token on the setup page to create your owner login. Keep the token
private. This profile has one local owner and a separate constrained agent
credential; [organization authentication](oidc-pilot.md) is a separate setup.

## Connect one example source

Follow Setup and choose **Local files**. Select one copied example folder, such
as `northstar`, and start discovery. Wait for discovery to complete, then open
the source's observed assets and evidence. A successful connection establishes
observations; it does not approve a definition, relationship or metric.

The default installation enables local files. To add your own files, copy
approved folders into the installation's `sources/` directory and choose their
relative paths. Check [source access and retention](architecture.md) before
using real records. Remote connectors need operator configuration and their own
[qualification checks](native-connector-qualification.md).

## Choose your agent client

Run the command for the client you use. Replace `/absolute/path/to/project`
with an existing project directory:

```sh
# Codex
./knowledge-layer agent --client codex --project /absolute/path/to/project

# Claude Code
./knowledge-layer agent --client claude --project /absolute/path/to/project

# Cursor
./knowledge-layer agent --client cursor --project /absolute/path/to/project
```

The launcher adds the Model Context Protocol (MCP) connection while preserving
other configured servers.
Follow its printed instruction to load `KNOWLEDGE_AGENT_TOKEN` from the private
credential file into the environment of the client. Confirm the client's server
trust prompt. For Codex, trust the project and run `codex mcp list` there to
check that `knowledge_layer` appears.

The connection command configures MCP; the [agent skill guide](agent-skill-install.md)
describes the separate optional skill installation and other client setups.
A saved configuration is not proof that the client loaded it. Ask your agent:

> Call `get_build_handoff` and tell me which sources are visible, their current
> discovery and publication states, and what evidence-backed work remains.

Confirm that the response names your example source and its actual snapshot.
The default agent can draft knowledge but cannot create or refresh sources,
accept claims or publish them. Those owner actions stay in the browser.

## Build a small set of drafts

Ask your agent to work on the selected example source:

> Build a small set of semantic drafts for this source. Cite the current evidence
> for each definition, grain, relationship or metric. Preserve conflicts, leave
> ambiguous claims unresolved, and tell me exactly what I need to review.

For saved progress, start a run for the selected source in the **Build** pane
and use the [durable build flow](agent-build-flow.md). Starting a run saves the
work plan; your configured agent or optional worker must execute it. A completed
run records draft work and blockers, independently of human acceptance.

## Review, publish and retrieve

Open the proposed claims in the browser and inspect their cited evidence.
Choose a small set you can support, preview the exact selection, and use
**Accept only**. To publish eligible current context in the same flow, use
**Accept and publish**. If publication is blocked, resolve the displayed blocker
and publish the reviewed snapshot afterward. See the
[publication rules](source-publication.md) for the exact eligibility conditions.

Ask the same agent to retrieve the reviewed context with `resolve_context` or
`get_contract` and report its source snapshot, evidence and trust states. Check
the returned `agent_ready` status. Publication permits use of the current
reviewed context; pending claims still remain drafts. Source changes or stale
evidence can suspend readiness and require fresh review.

Explore the [knowledge graph](collective-knowledge-graph.md) to follow a term
through its source bindings and evidence. Matching labels alone do not prove
identity or a valid join. Any query execution and analytical verification belong
to separately authorized source tools.

## Check, stop and continue

```sh
./knowledge-layer status
./knowledge-layer doctor
./knowledge-layer stop
```

The installation stores durable state outside the checkout. Stop it when you
are finished and start it again with `./knowledge-layer start`. The
[self-hosting guide](self-host.md) covers backup, recovery and alternate ports.

This walkthrough uses synthetic examples to learn an alpha workflow. It does
not qualify customer permissions, every agent client or production operation.
Continue with the [documentation index](README.md), or ask a usage question in
[GitHub Discussions](https://github.com/Quiet-Victory-Labs/knowledge-layer/discussions).
