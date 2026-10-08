# Build with your agents, review once, reuse the knowledge

The intended customer flow is: clone and deploy Knowledge Layer, connect the agents you already use, connect permission-scoped data sources, ask those agents to build the knowledge, review their drafts, and use the same agents to ask questions across your data. Models run in the customer's harness. Knowledge Layer stores the evidence, semantic drafts, decisions and published context.

## Start and connect

Run `./scripts/start.sh` for the local container stack, or deploy the service and PostgreSQL on customer infrastructure using the [cloud deployment guide](cloud-deployment.md) and [OIDC pilot](oidc-pilot.md). Configure the protected MCP endpoint and install the [portable knowledge-layer skill](agent-skill-install.md) in Codex, Cursor, Claude Code or another compatible harness. For remote agents, the operator publishes an authenticated HTTPS endpoint. A configured MCP URL or installed skill still needs to be loaded by the chosen client.

Connect the requested structured and unstructured sources using enabled connectors. Warehouses, databases, object storage, mounted files, PDF/document collections and customer-exported catalogs can contribute observed context through their supported connector or [external adapter](external-connector-protocol.md). The operator provides restricted credentials, mounts, network access and connector profiles; agents select approved scopes and never receive credentials in prompt text. A provider appearing in documentation does not qualify every native vendor account or permission model. Native Google Drive and opt-in remote document retention are described in the remote document guide; other SaaS integrations can use separately deployed adapters.

Ask the agent:

> Build the knowledge layer for these connected sources. Work through their metadata, ontology, glossary, source contracts and supported metric definitions. Cite evidence, preserve conflicting definitions, and leave ambiguous claims unresolved. Prepare the drafts for me to review and accept. Tell me which sources or work remain incomplete.

Choose exact sources, or explicitly authorize all connections available to that agent. Source permissions continue to restrict every returned page.

## Read the build handoff

Compact MCP exposes `get_build_handoff(connection_id=None, cursor=None, limit=20)`. HTTP exposes `GET /agent/api/build-handoff` with the same parameters. The handoff works when source-management and delegated-autonomy tools are disabled; it grants no additional source access or mutation authority.

The handoff derives its state from persisted current snapshots, proposals, decisions and publication state. Each source includes:

- Current snapshot ID/version, discovery state, source errors and freshness clocks.
- A small sample from both meaning and contract queues, with exact stored evidence and snapshot-bound local citation references.
- Navigation to `next_work` or the corresponding HTTP work queue, plus count/scan limits and whether remaining work is known.
- Human review/publication state, blockers and the same-agent path to consume current context afterward.

Pass `next_cursor` to continue source pages. Each track's `navigation` points to the full bounded work API for that source; sampled packets alone do not cover all work. Work APIs retain their own paging and capacity limits. Exact evidence means the returned stored record is complete; it does not imply exhaustive extraction, OCR, complete source discovery or correct interpretation.

Local `evidence_ids` identify the source snapshot's evidence. `local_evidence_refs` include their connection/snapshot pins for navigation. Cross-source proposals still use the separate `evidence_refs` rules in the skill; copying local references into that field does not create a cross-source citation.

The handoff retains full sampled evidence text. Complete-response budgets refuse oversized HTTP/MCP payloads instead of clipping claims or citations. Reduce the source-page limit or select one connection after a refusal. The underlying maintenance projection also has its own bounded scan and response budget.

## Build, accept and consume

Start a durable build for selected sources in the **Build** pane or with `build_knowledge`. The customer agent claims exact pinned evidence packets from both queues, proposes supported meaning/contracts, and checkpoints each proposal, abstention or blocker. Draft and checkpoint commit atomically. A process restart resumes the remaining work; expired leases are reclaimed within a finite retry budget. Changed source snapshots invalidate outstanding claims.

The [durable build guide](durable-agent-builds.md) supplies client setup, MCP/HTTP operations, and an optional worker using an installed Codex/Claude/Cursor CLI or a compatible model endpoint. The core service stores and coordinates work without a model dependency. Start saves a run; a configured customer harness or worker must execute it. Discovery has its separate durable refresh worker. Completed runs retain their uncertainty receipts and do not silently relaunch when a human reviews them. Budgets and incomplete enumeration remain visible.

Review the proposed claims and their evidence in the browser. The selection preview pins the proposal IDs and source snapshot and returns a digest bound to those exact claims and their local evidence. **Accept selected** validates that preview and commits the selected approvals together with one resulting review snapshot. A changed selection, snapshot, claim or evidence requires a new preview; no part of an invalid selection is accepted.

A steward can choose **Accept and publish** to accept that selection and then apply the existing publication gate to the resulting reviewed snapshot. If publication becomes blocked after acceptance, the response reports `accepted_publication_blocked`: the completed review remains recorded, while agent publication is still unavailable. Resolve the returned blocker and publish the current reviewed snapshot afterward. Selection covers only those exact claims. Outstanding drafts and ambiguities remain visible with their own trust states; publication does not certify complete company knowledge.

The same agent then uses `resolve_context`, `get_contract` or `get_task_context` to retrieve current scoped definitions, glossary bindings, contracts, metrics and guidance. It revalidates task receipts and source dependencies before acting. The customer's separately authorized source tools execute queries and verify results. A reviewed metric definition provides meaning and query guidance; it is distinct from a computed analytical result.

Persistent abstention receipts, provider/harness resumption and whole-estate workload orchestration remain follow-on work. The optional [reference model curator](model-neutral-curation.md) currently processes meaning packets only; the portable agent skill describes the complete two-track customer-harness flow.
