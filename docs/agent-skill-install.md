# Agent-first customer workflow

Native MCP consumption is qualified separately from skill activation. See the
[native CLI drill](native-harness-qualification.md) for the scoped Codex fixture
result and reproducible client checks. Exported files alone do not prove that a
client discovered or applied a skill.

The [portable knowledge-layer skill](../skills/knowledge-layer/SKILL.md) tells an end user's agent how to connect a source, curate evidence-backed meaning, and report what remains unresolved. The model runs in Codex, Claude Code, Cursor, Grok Bot, or another MCP client. The platform provides the tools and stores the knowledge; it does not require the customer to use an in-app chat.

## Run the local demo

Start the lightweight stack with `./scripts/start.sh`. The compact protected MCP endpoint is `http://127.0.0.1:4173/agent/mcp/`. Give the client the generated `KNOWLEDGE_AGENT_TOKEN` through its credential store or process environment. Do not paste it into a prompt or check it into a client configuration file. A remote agent needs an operator-published HTTPS endpoint; localhost is reachable only to agents on this computer. The detailed tools remain at `/agent/advanced-mcp/`.

The lightweight demo enables agent source management **only for Local files**. The operator mounts `data/connectable/` read-only into the service. The agent may choose a contained folder under that root but cannot upload an arbitrary laptop folder or supply a database password in a tool call. For a database, warehouse, object store, or SaaS adapter, an operator must first provision a scoped `KNOWLEDGE_SOURCE_*` secret reference and explicitly enable that connector kind; the connector still needs provider qualification before customer use. The [OIDC pilot](oidc-pilot.md) enforces explicit source metadata grants for browser and agent reads. Source-native row/document and field permissions are still a deployment gate.

In the OIDC pilot, `connect_source` and `refresh_source` return a durable job. The agent should use `get_refresh_job` once after a reasonable interval and resume curation only when it succeeds. The local demo returns the completed discovery response immediately.

Source lists use bounded pages. `get_workspace` defaults to 20 connections, detailed `list_knowledge_sources` and HTTP `GET /agent/api/sources` default to 30, and each returns `next_cursor` when more authorized sources remain. Pass that opaque cursor to continue; a first page with no named source does not establish that the source is absent. The browser loads 50 sources initially and offers “Load more sources.” Counts are for the current authorized principal; page membership can change when sources or grants change.

The agent can draft evidence-backed assertions but cannot publish a source. An owner reviews individual assertions and a steward may [publish the current snapshot](source-publication.md) from the browser. Agents must use the returned `agent_ready` flag and each assertion's trust state; a connector's own readiness claim has no authority.

Install the skill in the end user's project:

```sh
uv run --no-sync python scripts/install_agent_skill.py --client portable --project /path/to/customer-project
uv run --no-sync python scripts/install_agent_skill.py --client codex --project /path/to/customer-project
uv run --no-sync python scripts/install_agent_skill.py --client claude --project /path/to/customer-project
uv run --no-sync python scripts/install_agent_skill.py --client cursor --project /path/to/customer-project
```

Run only the command for the client being used. Portable and Codex installations use `.agents/skills/knowledge-layer/`; Claude Code uses `.claude/skills/knowledge-layer/`; Cursor uses `.cursor/skills/knowledge-layer/`. These locations follow the current [Codex skill documentation](https://developers.openai.com/codex/skills/), [Claude Code skill documentation](https://code.claude.com/docs/en/skills), and [Cursor Agent Skills documentation](https://cursor.com/docs/skills). A portable folder can be read by another harness that supports the [Agent Skills specification](https://agentskills.io/specification); directory discovery and activation remain that harness's responsibility. This repo has not qualified an actual session in each client.

The installer leaves an existing different skill untouched. `--replace` prepares the new copy first, then preserves the previous entire directory or target symlink in a dated backup. It never follows a symlink in an installation parent or in the source skill. It does not change `AGENTS.md`, `CLAUDE.md`, MCP configuration, existing company rules or tool permissions. Use `--scope user` for a personal installation. For a harness with a custom discovery directory, provide the full skill folder explicitly:

```sh
uv run --no-sync python scripts/install_agent_skill.py --client portable --destination /path/to/custom-skills/knowledge-layer
```

The destination must end in `knowledge-layer`, matching the static skill's frontmatter name. An explicit destination overrides the client and scope placement. Parent traversal is refused. A historical `.codex/skills/knowledge-layer/` installation is preserved; this installer neither migrates nor deletes it. Check for duplicate skills when adopting the current `.agents/skills/` location.

## Export published company skills and rules

The static skill above describes how to use the knowledge layer. Company guidance is a separate, versioned registry: a source manager can draft procedures and rules, while a steward publishes a reviewed revision. Published guidance can describe a company's research or operational procedure without claiming that a draft, source observation or imported upstream approval is an instruction.

Use `resolve_guidance` for the requested business unit, task and source scope. Read the complete resolution and its conflicts. Only a `ready` resolution can be exported through `export_guidance_bundle` or `GET /agent/api/guidance/export` with the same context and `client=portable`. Agents have no publication or revocation tool. Publication remains separate from source approval and from permission to execute queries.

The pure Python `export_guidance(resolution, client="portable")` returns a manifest and a map of relative file paths to complete UTF-8 content. It writes nothing. The manifest preserves published revisions, canonical payload digests, audience and applicability, exact snapshot dependencies, publication provenance, evaluation time and effective dates. Applicability `connection_ids` and pinned `dependencies` remain separate. It declares `enforcement=guidance_only` and `revalidate_before_use=true`.

Each company skill is namespaced under `.agents/skills/kl-<bundle>-<skill>/SKILL.md` with valid YAML `name` and `description`. Long names receive a stable hash suffix rather than colliding through truncation. Each skill contains its full instructions and every rule in the applicable resolution, including JSON scalar values such as null, false, zero or an empty string. A local `references/company-guidance.md` carries live registry bindings and revalidation instructions. `knowledge-layer-guidance/BOOTSTRAP.md` provides the same bootstrap and company rules without requiring a particular model or harness. The optional `codex`, `claude` and `cursor` formats change only the skill directory; they generate no native permission configuration or executable scripts.

Before applying an exported skill, fetch the current authorized resolution again and verify its revisions, digests, validity and readable dependencies. Stop when the registry is unavailable or the guidance is revoked, expired, stale, conflicted or inaccessible. A cached export cannot establish current permission or current company policy. Required capabilities are descriptive prerequisites; they never become `allowed-tools`, automatic grants or enabled connectors. The export describes applicable guidance, not every policy across the company.

The exporter refuses an entire blocked, empty, malformed, conflicting, stale or overbudget resolution. It also refuses invalid names, collisions, credential material and payloads that differ from their published digests. The complete export is capped at 20 bundles, 100 skills and 256 KiB; narrow the requested context if it exceeds that budget. Successful exports retain full text. No rule, instruction or unresolved conflict is silently omitted. Installing or activating the result is a customer-controlled harness step; preserve existing user rules and review any conflicts instead of rewriting them.

The format and relative references follow the [Agent Skills specification](https://agentskills.io/specification) and [client integration guidance](https://agentskills.io/client-implementation/adding-skills-support). A provider may support different discovery, trust and activation behavior; generating the files is not proof that its harness has loaded or enforced them.

Configure the same MCP URL in the chosen client. Examples below are templates; set the environment variable from a trusted secret store before starting the client.

**Codex** — in the user's Codex config:

```toml
[mcp_servers.knowledge_layer]
url = "http://127.0.0.1:4173/agent/mcp/"
bearer_token_env_var = "KNOWLEDGE_AGENT_TOKEN"
```

**Claude Code** — in the user's project `.mcp.json`:

```json
{
  "mcpServers": {
    "knowledge-layer": {
      "type": "http",
      "url": "http://127.0.0.1:4173/agent/mcp/",
      "headers": {"Authorization": "Bearer ${KNOWLEDGE_AGENT_TOKEN}"}
    }
  }
}
```

Check `/mcp` after launch. Claude Code supports project HTTP MCP configuration and environment references in headers; see its [MCP documentation](https://code.claude.com/docs/en/mcp).

**Cursor** — in the user's project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "knowledge-layer": {
      "url": "http://127.0.0.1:4173/agent/mcp/",
      "headers": {"Authorization": "Bearer ${env:KNOWLEDGE_AGENT_TOKEN}"}
    }
  }
}
```

Restart Cursor and confirm the MCP server is available. Cursor documents [remote MCP headers and environment interpolation](https://prod.cursor.com/docs/mcp).

**Grok Bot** — add the published HTTPS MCP endpoint as a custom Remote HTTPS plugin, store its scoped credential in the Bot setup, and add the skill text as a Bot skill. Its [Team Bot documentation](https://docs.x.ai/grok-bot/team-bots) describes shared skills and custom MCP plugins. This repo has not connected a live Grok Bot account.

Then ask the agent: “Use the knowledge-layer skill to connect the Northstar Field Services sample, build the evidence-backed semantic layer, and tell me what still needs owner review.” A successful run uses `get_workspace`, `connect_source`, `next_work`, and evidence-backed proposal tools. For policy documents in another connection, it uses `search_evidence` with `scope=documents` and snapshot-pinned citations. It reports the returned snapshot, pending proposals, and actual `agent_ready` state. It never reports candidate meaning as certified.

The same skill works for a later refresh: “Refresh this source, inspect what changed, and revise only the affected knowledge.” Tool access and scheduling depend on the client. The platform's `refresh_source` reuses unchanged profiles and builds new candidate work for changed assets.

For additive project MCP setup, run `python3 scripts/configure_agent.py --client codex|claude|cursor --project /path/to/project --url https://your-host/agent/mcp/` (choose one client). The [durable build guide](durable-agent-builds.md) explains leases, saved checkpoints and optional unattended workers.
