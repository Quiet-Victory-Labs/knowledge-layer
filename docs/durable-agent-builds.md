# Durable builds with your agents

A build saves a scope, a wall-clock and item budget, both meaning and contract queues, and one receipt per agent outcome in PostgreSQL (SQLite is available for local evaluation). It has no model dependency. The same Codex, Claude, Cursor or HTTP agent can resume it after a process restart. Agents produce drafts; humans retain acceptance and publication.

## Connect the client

After `./scripts/start.sh`, configure the chosen project without copying credentials into files:

```sh
python3 scripts/configure_agent.py --client codex --project /path/to/project
python3 scripts/install_agent_skill.py --client codex --scope project --project /path/to/project
```

Use `--client claude` or `--client cursor` for those clients. For a remote installation add `--url https://your-host/agent/mcp/`. Existing unrelated MCP servers are preserved; replacement requires `--replace` and makes a backup. Expose `KNOWLEDGE_AGENT_TOKEN` through the client's environment/credential store. In the OIDC pilot this must be a valid audience/scoped agent token with source grants, refreshed by the customer. Load the server and confirm your client's trust prompts. Installation writes configuration; availability and skill activation must be checked in the selected client.

## Build and checkpoint

Connect and discover sources, then choose a source in **Build** and choose **Build**. **Details** holds agent setup, multi-source selection, saved runs, limits and outcomes. The default budget is 200 tasks and 60 minutes; the maximum is 500 tasks and 24 hours. Start saves work. A customer agent or separately configured worker executes it. A source without complete discovery waits; its existing discovery worker or `refresh_source` must finish before drafting begins.

Both MCP endpoints expose one `build_knowledge(operation, arguments)` tool:

| Operation | Arguments | Result |
| --- | --- | --- |
| `start` | `connection_ids`, optional `max_items`, `max_seconds` | `build` with durable ID |
| `list` | optional `connection_id`, `limit` | authorized summaries of persisted checkpoints |
| `get` | `run_id` | counts, source pins, coverage, outcomes, resume instruction |
| `claim` | `run_id`, optional `lease_seconds` (30–900, default 300) | one exact evidence packet and a lease token, or null task and state |
| `complete` | `run_id`, `task_id`, `lease_token`, `result` | atomic draft/checkpoint receipt |
| `cancel` | `run_id`, `reason` | saved cancellation; old leases cannot write |

For example:

```json
{"operation":"claim","arguments":{"run_id":"build_REPLACE_WITH_REAL_ID"}}
```

For each claimed task, use its exact stored evidence. Checkpoint with `result.action=propose`, `reason` (8–1000 characters), `evidence_ids`, and either `proposed_definition`/`proposed_mapping` for meaning or a typed contract `payload`. The packet identifies the exact target, assertion kind and current source snapshot/version. For `review_relationship`, the mapping must equal `candidate.proposed_mapping_shape`. Return only `action` and `reason` for `abstain`, `defer` or `error`. Do not claim more work than the agent can finish inside its lease.

Repeat claim/checkpoint until the tool returns no task. A crash loses no completed receipt; an expired lease is reclaimable up to three attempts. Repeating the identical completion under the same lease returns the existing receipt. A different completion cannot overwrite it. The draft and checkpoint commit together. Changed source snapshots invalidate outstanding leases and enqueue current work within the original item budget. Human review and publication remain separate.

HTTP equivalents live at `/agent/api/builds`, `/{id}`, `/{id}/claim`, `/{id}/complete` and `/{id}/cancel`. The browser exposes start/list/get/cancel, without agent lease/completion routes. Lists return bounded persisted summaries (`state_basis=last_checkpoint`); get and claim reconcile current discovery, evidence pins, leases and publication. This avoids repeatedly loading every full evidence packet just to list runs. Access to every source in a run is required to view or execute it. Lease tokens are bound to the authenticated agent subject and cannot be transferred between principals.

## Optional unattended worker

Run the customer-owned worker alongside the service. It consumes the same HTTP contract, covering both tracks and persisting abstentions and blockers. It starts no model server and requires no additional core-service process.

Using an existing native CLI login:

```sh
export KNOWLEDGE_GATEWAY_URL=http://127.0.0.1:4173
export KNOWLEDGE_AGENT_TOKEN_FILE=/private/path/to/agent-token
uv run python -m knowledge_layer.build_worker --client codex --run-id build_ACTUAL_ID
```

Choose `--client claude` or `--client cursor` for those installed clients. Each packet runs in a temporary workspace; the worker sends evidence through standard input, removes gateway/source secrets from the child environment, uses read-only Codex/Cursor modes or disables Claude built-in tools, and accepts bounded JSON decisions. Native authentication remains with the selected client. No force/permission-bypass flags are used. Account-specific prompts or incompatible CLI output become explicit errors. Offline adapter tests cover all three command paths.

On October 4, 2026, a real Codex CLI worker completed ten tasks against an isolated local CSV/document fixture: two supported contract drafts and eight explicit abstentions, with no errors. Exact human review and publication accepted the two drafts, and the same Codex client retrieved the published contract through MCP with the remaining limits intact. This qualifies that synthetic workflow. Live Claude/Cursor execution, cloud-account IAM and customer-source behavior remain separate qualification gates.

For a compatible Chat Completions endpoint:

```sh
export MODEL_CHAT_COMPLETIONS_URL=https://your-provider/v1/chat/completions
export MODEL_NAME=your-chosen-model
export MODEL_API_KEY_FILE=/private/path/to/model-token
export MODEL_ALLOW_REMOTE=1
uv run python -m knowledge_layer.build_worker --run-id build_ACTUAL_ID
```

`MODEL_ALLOW_REMOTE=1` authorizes sending selected source evidence to that provider. Local endpoints use loopback HTTP; remote endpoints require HTTPS and redirects are refused. Model output cannot approve or publish. Invalid drafts are recorded as deferred, provider failures as errors, and changed leases as stale/expired outcomes. Logs contain run IDs, states and counts rather than evidence or credentials.

`--watch` watches currently visible active builds; `--max-seconds` bounds the worker process lifetime (default one hour, maximum one day). `--interval` is 5–300 seconds. The gateway token file is reread between cycles for customer-managed rotation. A service manager may restart the worker; each build retains its own original budget. `deploy/compose.builder.yaml` is an opt-in model-worker overlay for the OIDC pilot with scoped token files. Mount regular mode-0600 files readable by the pilot’s configured run UID; the overlay uses the same UID/GID and preserves owner-only permissions. The core stack still runs without a model or worker. Native workers run on the customer's host with that client's login.

To let UI starts be picked up automatically by your logged-in Codex client, replace `--run-id` with a bounded watch:

```sh
uv run python -m knowledge_layer.build_worker --client codex --watch --max-seconds 86400
```

For the model worker, supply the pilot environment and builder variables above, then apply the overlay from the repository root:

```sh
docker compose --env-file /private/path/to/pilot.env \
  -f deploy/compose.yaml -f deploy/compose.builder.yaml up -d builder
```

The watcher exits when its process budget ends. Arrange the next bounded invocation through your existing service manager if you need continuous coverage.

## Review and use

The progress pane shows queued, waiting, running, awaiting review, needs attention, cancelled, budget reached or published states. Counts include proposed, abstained, deferred, errored and stale work. Exact stored evidence packets are limited to 64 KiB; oversized packets are deferred rather than truncated. Each track enumerates at most 500 source work items. Larger scopes or exhausted budgets are reported incomplete and require another bounded build or narrower connection scope. The registry caps stored runs/tasks; operators must apply a deliberate retention policy as usage grows.

Select supported drafts in **Review**, then choose **Review selected** to inspect the exact claims and cited evidence. Acknowledge the review and choose **Accept** or **Accept and publish**; an optional note replaces the acknowledgement statement in the audit rationale. Accepted claims and a review snapshot commit atomically; publication uses the existing steward gate. A proposal/evidence/snapshot change requires a fresh preview. Outstanding drafts and abstentions remain visible. A source becoming published does not certify exhaustive knowledge, units, joins or business meaning.

The same agents then use `resolve_context`, `get_contract`, `get_task_context`, metric/query planning and ontology export. Knowledge Layer supplies permission-scoped, reviewed meaning and provenance. Actual database execution stays in the customer's separately authorized source tools; the agent revalidates current schema, receipts, grain, cohort and results there.
